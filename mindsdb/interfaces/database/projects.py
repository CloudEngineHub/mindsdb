import datetime
from copy import deepcopy
from typing import List, Optional
from collections import OrderedDict

import sqlalchemy as sa
import numpy as np

from mindsdb_sql_parser.ast.base import ASTNode
from mindsdb_sql_parser.ast import Select, Star, Constant, Identifier
from mindsdb_sql_parser import parse_sql

from mindsdb.interfaces.storage import db
from mindsdb.interfaces.model.model_controller import ModelController
from mindsdb.interfaces.database.views import ViewController
from mindsdb.utilities import log
from mindsdb.utilities.config import Config
from mindsdb.utilities.context import context as ctx
from mindsdb.utilities.exception import EntityExistsError, EntityNotExistsError
import mindsdb.utilities.profiler as profiler
from mindsdb.api.executor.sql_query import SQLQuery
from mindsdb.api.executor.utilities.sql import query_df
from mindsdb.interfaces.query_context.context_controller import query_context_controller

logger = log.getLogger(__name__)


class Project:
    @staticmethod
    def from_record(db_record: db.Project):
        p = Project()
        p.record = db_record
        p.name = db_record.name
        p.company_id = ctx.company_id
        p.id = db_record.id
        p.metadata = db_record.metadata_
        return p

    def create(self, name: str):
        name = name.lower()

        company_id = ctx.company_id if ctx.company_id is not None else 0

        existing_record = db.Integration.query.filter(
            sa.func.lower(db.Integration.name) == name, db.Integration.company_id == ctx.company_id
        ).first()
        if existing_record is not None:
            raise EntityExistsError("Database exists with this name ", name)

        existing_record = db.Project.query.filter(
            (sa.func.lower(db.Project.name) == name)
            & (db.Project.company_id == company_id)
            & (db.Project.deleted_at == sa.null())
        ).first()
        if existing_record is not None:
            raise EntityExistsError("Project already exists", name)

        record = db.Project(name=name, company_id=company_id)

        self.record = record
        self.name = name
        self.company_id = company_id

        db.session.add(record)
        db.session.commit()

        self.id = record.id

    def delete(self):
        if self.record.metadata_ and self.record.metadata_.get("is_default", False):
            raise Exception(
                f"Project '{self.name}' can not be deleted, because it is default project."
                "The default project can be changed in the config file or by setting the environment variable MINDSDB_DEFAULT_PROJECT."
            )

        tables = self.get_tables()
        tables = [key for key, val in tables.items() if val["type"] != "table"]
        if len(tables) > 0:
            raise Exception(
                f"Project '{self.name}' can not be deleted, because it contains tables: {', '.join(tables)}"
            )

        is_cloud = Config().get("cloud", False)
        if is_cloud is True:
            self.record.deleted_at = datetime.datetime.now()
        else:
            db.session.delete(self.record)
            self.record = None
            self.name = None
            self.company_id = None
            self.id = None
        db.session.commit()

    def drop_model(self, name: str):
        ModelController().delete_model(name, project_name=self.name)

    def drop_view(self, name: str, strict_case: bool = False) -> None:
        """Remove a view with the specified name from the current project.

        Args:
            name (str): The name of the view to remove.
            strict_case (bool, optional): If True, the view name is case-sensitive. Defaults to False.

        Raises:
            EntityNotExistsError: If the view does not exist.

        Returns:
            None
        """
        ViewController().delete(name, project_name=self.name, strict_case=strict_case)

    def create_view(self, name: str, query: str):
        ViewController().add(name, query=query, project_name=self.name)

    def update_view(self, name: str, query: str, strict_case: bool = False):
        ViewController().update(name, query=query, project_name=self.name, strict_case=strict_case)

    def delete_view(self, name: str):
        ViewController().delete(name, project_name=self.name)

    def get_view_meta(self, query: ASTNode) -> ASTNode:
        view_name = query.from_table.parts[-1]
        view_meta = ViewController().get(name=view_name, project_name=self.name)
        view_meta["query_ast"] = parse_sql(view_meta["query"])
        return view_meta

    def query_view(self, query, session):
        view_meta = self.get_view_meta(query)

        query_context_controller.set_context("view", view_meta["id"])

        try:
            sqlquery = SQLQuery(view_meta["query_ast"], session=session)
            df = sqlquery.fetched_data.to_df()
        finally:
            query_context_controller.release_context("view", view_meta["id"])

        # remove duplicated columns
        df = df.loc[:, ~df.columns.duplicated()]

        return query_df(df, query, session=session)

    @staticmethod
    def _get_model_data(predictor_record, integraion_record, with_secrets: bool = True):
        from mindsdb.interfaces.database.integrations import integration_controller

        predictor_data = predictor_record.data or {}
        training_time = None
        if (
            predictor_record.training_start_at is not None
            and predictor_record.training_stop_at is None
            and predictor_record.status != "error"
        ):
            training_time = round((datetime.datetime.now() - predictor_record.training_start_at).total_seconds(), 3)
        elif predictor_record.training_start_at is not None and predictor_record.training_stop_at is not None:
            training_time = round(
                (predictor_record.training_stop_at - predictor_record.training_start_at).total_seconds(), 3
            )

        # regon Hide sensitive info
        training_options = predictor_record.learn_args
        handler_module = integration_controller.get_handler_module(integraion_record.engine)

        if with_secrets is False and handler_module:
            model_using_args = getattr(handler_module, "model_using_args", None)
            if (
                isinstance(model_using_args, dict)
                and isinstance(training_options, dict)
                and isinstance(training_options.get("using"), dict)
            ):
                training_options["using"] = deepcopy(training_options["using"])
                for key, value in model_using_args.items():
                    if key in training_options["using"] and value.get("secret", False):
                        training_options["using"][key] = "******"
        # endregion

        predictor_meta = {
            "type": "model",
            "id": predictor_record.id,
            "engine": integraion_record.engine,
            "engine_name": integraion_record.name,
            "active": predictor_record.active,
            "version": predictor_record.version,
            "status": predictor_record.status,
            "accuracy": None,
            "predict": predictor_record.to_predict[0],
            "update_status": predictor_record.update_status,
            "mindsdb_version": predictor_record.mindsdb_version,
            "error": predictor_data.get("error"),
            "select_data_query": predictor_record.fetch_data_query,
            "training_options": training_options,
            "deletable": True,
            "label": predictor_record.label,
            "current_training_phase": predictor_record.training_phase_current,
            "total_training_phases": predictor_record.training_phase_total,
            "training_phase_name": predictor_record.training_phase_name,
            "training_time": training_time,
        }
        if predictor_data.get("accuracies", None) is not None:
            if len(predictor_data["accuracies"]) > 0:
                predictor_meta["accuracy"] = float(np.mean(list(predictor_data["accuracies"].values())))
        return {"name": predictor_record.name, "metadata": predictor_meta, "created_at": predictor_record.created_at}

    def get_model(self, name: str):
        record = (
            db.session.query(db.Predictor, db.Integration)
            .filter_by(project_id=self.id, active=True, name=name, deleted_at=sa.null(), company_id=ctx.company_id)
            .join(db.Integration, db.Integration.id == db.Predictor.integration_id)
            .order_by(db.Predictor.name, db.Predictor.id)
            .first()
        )
        if record is None:
            return None
        return self._get_model_data(record[0], record[1])

    def get_model_by_id(self, model_id: int):
        record = (
            db.session.query(db.Predictor, db.Integration)
            .filter_by(project_id=self.id, id=model_id, deleted_at=sa.null(), company_id=ctx.company_id)
            .join(db.Integration, db.Integration.id == db.Predictor.integration_id)
            .order_by(db.Predictor.name, db.Predictor.id)
            .first()
        )
        if record is None:
            return None
        return self._get_model_data(record[0], record[1])

    def get_models(self, active: bool = True, with_secrets: bool = True):
        query = db.session.query(db.Predictor, db.Integration).filter_by(
            project_id=self.id, deleted_at=sa.null(), company_id=ctx.company_id
        )
        if isinstance(active, bool):
            query = query.filter_by(active=active)

        query = query.join(db.Integration, db.Integration.id == db.Predictor.integration_id).order_by(
            db.Predictor.name, db.Predictor.id
        )

        data = []

        for predictor_record, integraion_record in query.all():
            data.append(self._get_model_data(predictor_record, integraion_record, with_secrets))

        return data

    def get_agents(self):
        records = (
            db.session.query(db.Agents)
            .filter(
                db.Agents.project_id == self.id,
                db.Agents.company_id == ctx.company_id,
                db.Agents.deleted_at == sa.null(),
            )
            .order_by(db.Agents.name)
            .all()
        )
        data = [
            {
                "name": record.name,
                "query": record.query,
                "metadata": {"type": "agent", "id": record.id, "deletable": True},
            }
            for record in records
        ]
        return data

    def get_knowledge_bases(self):
        from mindsdb.api.executor.controllers.session_controller import SessionController

        session = SessionController()

        return {
            kb["name"]: {"type": "knowledge_base", "id": kb["id"], "deletable": True}
            for kb in session.kb_controller.list(self.name)
        }

    def get_views(self):
        records = (
            db.session.query(db.View)
            .filter_by(project_id=self.id, company_id=ctx.company_id)
            .order_by(db.View.name, db.View.id)
            .all()
        )
        data = [
            {
                "name": view_record.name,
                "query": view_record.query,
                "metadata": {"type": "view", "id": view_record.id, "deletable": True},
            }
            for view_record in records
        ]
        return data

    def get_view(self, name: str, strict_case: bool = False) -> dict | None:
        """Get a view by name from the current project.

        Args:
            name (str): The name of the view to retrieve.
            strict_case (bool, optional): If True, the view name is case-sensitive. Defaults to False.

        Returns:
            dict | None: A dictionary with view information if found, otherwise None.
        """
        query = db.session.query(db.View).filter(
            db.View.project_id == self.id,
            db.View.company_id == ctx.company_id,
        )
        if strict_case:
            query = query.filter(db.View.name == name)
        else:
            query = query.filter(sa.func.lower(db.View.name) == name.lower())

        view_record = query.one_or_none()

        if view_record is None:
            return None
        return {
            "name": view_record.name,
            "query": view_record.query,
            "metadata": {"type": "view", "id": view_record.id, "deletable": True},
        }

    @profiler.profile()
    def get_tables(self):
        data = OrderedDict()
        data["models"] = {"type": "table", "deletable": False}

        models = self.get_models()
        for model in models:
            if model["metadata"]["active"] is True:
                data[model["name"]] = model["metadata"]

        views = self.get_views()
        for view in views:
            data[view["name"]] = view["metadata"]

        agents = self.get_agents()
        for agent in agents:
            data[agent["name"]] = agent["metadata"]

        data.update(self.get_knowledge_bases())

        return data

    def get_columns(self, table_name: str) -> list[str] | None:
        columns = []
        tables = self.get_tables()
        table = None
        for key, value in tables.items():
            if key.lower() == table_name.lower():
                table_name = key
                table = value
        if table is None:
            return columns

        match str(table["type"]).upper():
            case "MODEL":
                predictor_record = db.Predictor.query.filter_by(
                    company_id=ctx.company_id, project_id=self.id, name=table_name
                ).first()
                columns = []
                if predictor_record is not None:
                    if isinstance(predictor_record.dtype_dict, dict):
                        columns = list(predictor_record.dtype_dict.keys())
                    elif predictor_record.to_predict is not None:
                        # no dtype_dict, use target
                        columns = predictor_record.to_predict
                        if not isinstance(columns, list):
                            columns = [columns]
            case "VIEW":
                query = Select(targets=[Star()], from_table=Identifier(table_name), limit=Constant(1))

                from mindsdb.api.executor.controllers.session_controller import SessionController

                session = SessionController()
                session.database = self.name
                df = self.query_view(query, session)
                columns = df.columns
            case "AGENT":
                agent = db.Agents.query.filter_by(
                    company_id=ctx.company_id, project_id=self.id, name=table_name
                ).first()
                if agent is not None:
                    from mindsdb.interfaces.agents.constants import ASSISTANT_COLUMN, USER_COLUMN

                    columns = [ASSISTANT_COLUMN, USER_COLUMN]
            case "KNOWLEDGE_BASE":
                columns = ["id", "chunk_id", "chunk_content", "metadata", "relevance", "distance"]
            case "TABLE":
                # like 'mindsdb.models'
                pass
            case _:
                logger.warning(f"Unknown table type: {table['type']}")

        return columns


class ProjectController:
    def __init__(self):
        pass

    def get_list(self) -> List[Project]:
        company_id = ctx.company_id if ctx.company_id is not None else 0
        records = db.Project.query.filter(
            (db.Project.company_id == company_id) & (db.Project.deleted_at == sa.null())
        ).order_by(db.Project.name)

        return [Project.from_record(x) for x in records]

    def get(
        self,
        id: int | None = None,
        name: str | None = None,
        deleted: bool = False,
        is_default: bool = False,
        strict_case: bool = False,
    ) -> Project:
        """Get a project by id or name.

        Args:
            id (int | None, optional): The id of the project to retrieve. Cannot be used with 'name'.
            name (str | None, optional): The name of the project to retrieve. Cannot be used with 'id'.
            deleted (bool, optional): If True, include deleted projects. Defaults to False.
            is_default (bool, optional): If True, only return the default project. Defaults to False.
            strict_case (bool, optional): If True, the project name is case-sensitive. Defaults to False.

        Raises:
            ValueError: If both 'id' and 'name' are provided.
            EntityNotExistsError: If the project is not found.

        Returns:
            Project: The project instance matching the given criteria.
        """
        if id is not None and name is not None:
            raise ValueError("Both 'id' and 'name' can't be provided at the same time")

        company_id = ctx.company_id if ctx.company_id is not None else 0
        q = db.Project.query.filter_by(company_id=company_id)

        if id is not None:
            q = q.filter_by(id=id)
        elif name is not None:
            if strict_case:
                q = q.filter((db.Project.name == name))
            else:
                q = q.filter((sa.func.lower(db.Project.name) == sa.func.lower(name)))

        if deleted is True:
            q = q.filter((db.Project.deleted_at != sa.null()))
        else:
            q = q.filter_by(deleted_at=sa.null())

        if is_default:
            q = q.filter(db.Project.metadata_["is_default"].as_boolean() == is_default)

        record = q.first()

        if record is None:
            raise EntityNotExistsError(f"Project not found: {name}")
        return Project.from_record(record)

    def add(self, name: str) -> Project:
        project = Project()
        project.create(name=name)
        return project

    def update(self, id: Optional[int] = None, name: Optional[str] = None, new_name: str = None) -> Project:
        if id is not None and name is not None:
            raise ValueError("Both 'id' and 'name' can't be provided at the same time")

        if id is not None:
            project = self.get(id=id)
        else:
            project = self.get(name=name)

        if new_name is not None:
            project.name = new_name
            project.record.name = new_name

        db.session.commit()
        return project
