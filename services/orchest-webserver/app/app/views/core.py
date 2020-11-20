import json
import os
import shutil
import uuid
import pdb
import requests
import logging
import nbformat
import docker
import subprocess

from sqlalchemy.sql.expression import not_
from flask import render_template, request, jsonify
from flask_restful import Api, Resource, HTTPException
from flask_marshmallow import Marshmallow
from distutils.dir_util import copy_tree
from nbconvert import HTMLExporter
from subprocess import Popen

from app.utils import (
    get_hash,
    get_user_conf,
    get_user_conf_raw,
    save_user_conf_raw,
    name_to_tag,
    find_pipelines_in_dir,
    pipeline_uuid_to_path,
    project_uuid_to_path,
    remove_dir_if_empty,
    get_pipeline_path,
    get_pipeline_directory,
    get_project_directory,
    get_environment_directory,
    get_environments,
    serialize_environment_to_disk,
    read_environment_from_disk,
    delete_environment,
)

from app.models import (
    DataSource,
    Experiment,
    Environment,
    PipelineRun,
    Project,
    Pipeline,
    BackgroundTask,
)

from app.schemas import (
    ProjectSchema,
    PipelineSchema,
    DataSourceSchema,
    EnvironmentSchema,
    ExperimentSchema,
    BackgroundTaskSchema,
)
from app.kernel_manager import populate_kernels
from app.views.orchest_api import api_proxy_environment_builds
from _orchest.internals.utils import run_orchest_ctl
from _orchest.internals import config as _config

logging.basicConfig(level=logging.DEBUG)


def register_views(app, db):
    errors = {
        "DataSourceNameInUse": {
            "message": "A data source with this name already exists.",
            "status": 409,
        },
    }

    api = Api(app, errors=errors)

    class DataSourceNameInUse(HTTPException):
        pass

    project_schema = ProjectSchema()
    projects_schema = ProjectSchema(many=True)

    pipeline_schema = PipelineSchema()
    pipelines_schema = PipelineSchema(many=True)

    datasource_schema = DataSourceSchema()
    datasources_schema = DataSourceSchema(many=True)

    environment_schema = EnvironmentSchema()
    environments_schema = EnvironmentSchema(many=True)

    experiment_schema = ExperimentSchema()
    experiments_schema = ExperimentSchema(many=True)

    background_task_schema = BackgroundTaskSchema()

    def register_environments(db, api):
        class EnvironmentsResource(Resource):
            def get(self, project_uuid):
                return environments_schema.dump(
                    get_environments(
                        project_uuid, language=request.args.get("language")
                    )
                )

        class EnvironmentResource(Resource):
            def put(self, project_uuid, environment_uuid):
                return self.post(project_uuid, environment_uuid)

            def get(self, project_uuid, environment_uuid):
                environment_dir = get_environment_directory(
                    environment_uuid, project_uuid
                )
                return environment_schema.dump(
                    read_environment_from_disk(environment_dir, project_uuid)
                )

            def delete(self, project_uuid, environment_uuid):

                delete_environment(project_uuid, environment_uuid)
                # refresh kernels after change in environments
                populate_kernels(app, db)

                return jsonify({"message": "Environment deletion was successful."})

            def post(self, project_uuid, environment_uuid):

                # create a new environment in the project
                environment_json = request.json.get("environment")

                e = Environment(
                    uuid=str(uuid.uuid4()),
                    name=environment_json["name"],
                    project_uuid=project_uuid,
                    language=environment_json["language"],
                    setup_script=environment_json["setup_script"],
                    base_image=environment_json["base_image"],
                    gpu_support=environment_json["gpu_support"],
                )

                # use specified uuid if it's not keyword 'new'
                if environment_uuid != "new":
                    e.uuid = environment_uuid

                environment_dir = get_environment_directory(e.uuid, project_uuid)

                os.makedirs(environment_dir, exist_ok=True)
                serialize_environment_to_disk(e, environment_dir)

                # refresh kernels after change in environments
                populate_kernels(app, db)

                return environment_schema.dump(e)

        api.add_resource(
            EnvironmentsResource, "/store/environments/<string:project_uuid>"
        )
        api.add_resource(
            EnvironmentResource,
            "/store/environments/<string:project_uuid>/<string:environment_uuid>",
        )

    def register_datasources(db, api):
        class DataSourcesResource(Resource):
            def get(self):

                show_internal = True
                if request.args.get("show_internal") == "false":
                    show_internal = False

                if show_internal:
                    datasources = DataSource.query.all()
                else:
                    datasources = DataSource.query.filter(
                        ~DataSource.name.like("\_%", escape="\\")
                    ).all()

                return datasources_schema.dump(datasources)

        class DataSourceResource(Resource):
            def put(self, name):
                ds = DataSource.query.filter(DataSource.name == name).first()

                if ds is None:
                    return "", 404

                ds.name = request.json["name"]
                ds.source_type = request.json["source_type"]
                ds.connection_details = request.json["connection_details"]
                db.session.commit()

                return datasource_schema.dump(ds)

            def get(self, name):
                ds = DataSource.query.filter(DataSource.name == name).first()

                if ds is None:
                    return "", 404

                return datasource_schema.dump(ds)

            def delete(self, name):
                ds = DataSource.query.filter(DataSource.name == name).first()

                if ds is None:
                    return "", 404

                db.session.delete(ds)
                db.session.commit()

                return jsonify({"message": "Data source deletion was successful"})

            def post(self, name):
                if DataSource.query.filter(DataSource.name == name).count() > 0:
                    raise DataSourceNameInUse()

                new_ds = DataSource(
                    name=name,
                    source_type=request.json["source_type"],
                    connection_details=request.json["connection_details"],
                )

                db.session.add(new_ds)
                db.session.commit()

                return datasource_schema.dump(new_ds)

        api.add_resource(DataSourcesResource, "/store/datasources")
        api.add_resource(DataSourceResource, "/store/datasources/<string:name>")

    def register_experiments(db, api):
        class ExperimentsResource(Resource):
            def get(self):
                experiments = Experiment.query.all()
                return experiments_schema.dump(experiments)

        class ExperimentResource(Resource):
            def put(self, experiment_uuid):

                ex = Experiment.query.filter(Experiment.uuid == experiment_uuid).first()

                if ex is None:
                    return "", 404

                ex.name = request.json["name"]
                ex.pipeline_uuid = request.json["pipeline_uuid"]
                ex.pipeline_name = request.json["pipeline_name"]
                ex.strategy_json = request.json["strategy_json"]
                ex.draft = request.json["draft"]

                db.session.commit()

                return experiment_schema.dump(ex)

            def get(self, experiment_uuid):
                ex = Experiment.query.filter(Experiment.uuid == experiment_uuid).first()

                if ex is None:
                    return "", 404

                return experiment_schema.dump(ex)

            def delete(self, experiment_uuid):

                # remove experiment directory
                ex = Experiment.query.filter(Experiment.uuid == experiment_uuid).first()

                if ex is None:
                    return "", 404

                remove_experiment_directory(ex.uuid, ex.pipeline_uuid, ex.project_uuid)

                db.session.delete(ex)
                db.session.commit()

                return jsonify({"message": "Experiment termination was successful"})

            def post(self, experiment_uuid):

                new_ex = Experiment(
                    uuid=experiment_uuid,
                    name=request.json["name"],
                    pipeline_uuid=request.json["pipeline_uuid"],
                    pipeline_name=request.json["pipeline_name"],
                    strategy_json=request.json["strategy_json"],
                    draft=request.json["draft"],
                )

                db.session.add(new_ex)
                db.session.commit()

                return experiment_schema.dump(new_ex)

        api.add_resource(ExperimentsResource, "/store/experiments")
        api.add_resource(
            ExperimentResource, "/store/experiments/<string:experiment_uuid>"
        )

    register_datasources(db, api)
    register_experiments(db, api)
    register_environments(db, api)

    def return_404(reason=""):
        json_string = json.dumps({"success": False, "reason": reason})

        return json_string, 404, {"content-type": "application/json"}

    def generate_gateway_kernel_name(project_uuid, environment_uuid):

        return _config.ENVIRONMENT_IMAGE_NAME.format(
            project_uuid=project_uuid, environment_uuid=environment_uuid
        )

    def build_environments(environment_uuids, project_uuid):
        project_path = project_uuid_to_path(project_uuid)

        environment_build_requests = [
            {
                "project_uuid": project_uuid,
                "project_path": project_path,
                "environment_uuid": environment_uuid,
            }
            for environment_uuid in environment_uuids
        ]

        return api_proxy_environment_builds(
            environment_build_requests, app.config["ORCHEST_API_ADDRESS"]
        )

    def build_environments_for_project(project_uuid):
        environments = get_environments(project_uuid)

        return build_environments(
            [environment.uuid for environment in environments], project_uuid
        )

    def populate_default_environments(project_uuid):

        for env_spec in app.config["DEFAULT_ENVIRONMENTS"]:
            e = Environment(**env_spec)

            e.uuid = str(uuid.uuid4())
            e.project_uuid = project_uuid

            environment_dir = get_environment_directory(e.uuid, project_uuid)
            os.makedirs(environment_dir, exist_ok=True)

            serialize_environment_to_disk(e, environment_dir)

    def pipeline_set_notebook_kernels(pipeline_json, pipeline_directory, project_uuid):

        # for each step set correct notebook kernel if it exists

        steps = pipeline_json["steps"].keys()

        for key in steps:
            step = pipeline_json["steps"][key]

            if "ipynb" == step["file_path"].split(".")[-1]:

                notebook_path = os.path.join(pipeline_directory, step["file_path"])

                if os.path.isfile(notebook_path):

                    gateway_kernel = generate_gateway_kernel_name(
                        project_uuid, step["environment"]
                    )

                    with open(notebook_path, "r") as file:
                        notebook_json = json.load(file)

                    notebook_changed = False

                    if (
                        notebook_json["metadata"]["kernelspec"]["name"]
                        != gateway_kernel
                    ):
                        notebook_changed = True
                        notebook_json["metadata"]["kernelspec"]["name"] = gateway_kernel

                    environment = (
                        Environment.query.filter(
                            Environment.uuid == step["environment"]
                        )
                        .filter(Environment.project_uuid == project_uuid)
                        .first()
                    )

                    if environment is not None:
                        if (
                            notebook_json["metadata"]["kernelspec"]["display_name"]
                            != environment.name
                        ):
                            notebook_changed = True
                            notebook_json["metadata"]["kernelspec"][
                                "display_name"
                            ] = environment.name
                    else:
                        logging.warn(
                            "Could not find environment [%s] while setting notebook kernelspec for notebook %s."
                            % (step["environment"], notebook_path)
                        )

                    if notebook_changed:
                        with open(notebook_path, "w") as file:
                            file.write(json.dumps(notebook_json, indent=2))

                else:
                    logging.info(
                        "pipeline_set_notebook_kernels called on notebook_path that doesn't exist %s"
                        % notebook_path
                    )

    def generate_ipynb_from_template(step, project_uuid):

        # TODO: support additional languages to Python and R
        if "python" in step["kernel"]["name"].lower():
            template_json = json.load(
                open(
                    os.path.join(app.config["RESOURCE_DIR"], "ipynb_template.json"), "r"
                )
            )
        else:
            template_json = json.load(
                open(
                    os.path.join(app.config["RESOURCE_DIR"], "ipynb_template_r.json"),
                    "r",
                )
            )

        template_json["metadata"]["kernelspec"]["display_name"] = step["kernel"][
            "display_name"
        ]
        template_json["metadata"]["kernelspec"]["name"] = generate_gateway_kernel_name(
            project_uuid, step["environment"]
        )

        return json.dumps(template_json, indent=2)

    def create_pipeline_files(pipeline_json, pipeline_directory, project_uuid):

        # Currently, we check per step whether the file exists.
        # If not, we create it (empty by default).
        # In case the file has an .ipynb extension we generate the file from a
        # template with a kernel based on the kernel description in the JSON step.

        # Iterate over steps
        steps = pipeline_json["steps"].keys()

        for key in steps:
            step = pipeline_json["steps"][key]

            file_name = step["file_path"]

            full_file_path = os.path.join(pipeline_directory, file_name)
            file_name_split = file_name.split(".")
            file_name_without_ext = ".".join(file_name_split[:-1])
            ext = file_name_split[-1]

            file_content = None

            if not os.path.isfile(full_file_path):

                if len(file_name_without_ext) > 0:
                    file_content = ""

                if ext == "ipynb":
                    file_content = generate_ipynb_from_template(step, project_uuid)

            elif ext == "ipynb":
                # check for empty .ipynb, for which we also generate a template notebook
                if os.stat(full_file_path).st_size == 0:
                    file_content = generate_ipynb_from_template(step, project_uuid)

            if file_content is not None:
                with open(full_file_path, "w") as file:
                    file.write(file_content)

    def create_experiment_directory(experiment_uuid, pipeline_uuid, project_uuid):

        experiment_path = os.path.join(
            app.config["USER_DIR"],
            "experiments",
            project_uuid,
            pipeline_uuid,
            experiment_uuid,
        )

        os.makedirs(experiment_path)
        snapshot_path = os.path.join(experiment_path, "snapshot")
        project_dir = os.path.join(
            app.config["USER_DIR"], "projects", project_uuid_to_path(project_uuid)
        )
        os.system("cp -R %s %s" % (project_dir, snapshot_path))

    def remove_experiment_directory(experiment_uuid, pipeline_uuid, project_uuid):

        experiment_project_path = os.path.join(
            app.config["USER_DIR"], "experiments", project_uuid
        )

        experiment_pipeline_path = os.path.join(experiment_project_path, pipeline_uuid)

        experiment_path = os.path.join(experiment_pipeline_path, experiment_uuid)

        if os.path.isdir(experiment_path):
            shutil.rmtree(experiment_path)

        # clean up parent directory if this experiment removal created empty directories
        remove_dir_if_empty(experiment_pipeline_path)
        remove_dir_if_empty(experiment_project_path)

    def cleanup_fs_removed_projects(fs_removed_projects):
        """Cleanup a project that was removed through the filesystem.

        Currently takes care of removing environment images from docker and the projects records from the db.
        Args:
            fs_removed_projects:

        Returns:

        """
        for removed_proj in fs_removed_projects:
            # TODO: make use of this in the PR where we completely cleanup a project and it's resources when it's
            #  deleted
            # remove experiments related to the project
            # exs_to_remove = Experiment.query.filter(Experiment.project_uuid == removed_proj.uuid).all()
            # for ex in exs_to_remove:
            #     remove_experiment_directory(ex.uuid, ex.pipeline_uuid, ex.project_uuid)
            #     db.session.delete(ex)

            # remove environment images related to the project
            try:
                requests.delete(
                    "http://"
                    + app.config["ORCHEST_API_ADDRESS"]
                    + "/api/environment-images/%s" % removed_proj.uuid
                )
            except Exception as e:
                logging.warning("Failed to delete EnvironmentImage: %s" % e)

            db.session.delete(removed_proj)
        db.session.commit()

        # refresh kernels after change in environments
        populate_kernels(app, db)

    def init_project(project_path: str):
        """Inits an orchest project.

        Given a directory it will detect what parts are missing from the .orchest directory for the project to
        be considered initialized, e.g. the actual .orchest directory, .gitignore file, environments directory, etc.
        As part of process initialization environments are built and kernels refreshed.

        Args:
            project_path: Directory of the project

        Returns:

        """
        projects_dir = os.path.join(app.config["USER_DIR"], "projects")
        full_project_path = os.path.join(projects_dir, project_path)

        new_project = Project(
            uuid=str(uuid.uuid4()),
            path=project_path,
        )
        db.session.add(new_project)
        db.session.commit()

        try:
            # this would actually be created as a collateral effect when populating with default environments,
            # let's not rely on that
            expected_internal_dir = os.path.join(full_project_path, ".orchest")
            if os.path.isfile(expected_internal_dir):
                raise NotADirectoryError(
                    "The expected internal directory (.orchest) is a file."
                )
            elif not os.path.isdir(expected_internal_dir):
                os.makedirs(expected_internal_dir)

            # init the .gitignore file if it is not there already
            expected_git_ignore_file = os.path.join(
                full_project_path, ".orchest", ".gitignore"
            )
            if os.path.isdir(expected_git_ignore_file):
                raise FileExistsError(".orchest/.gitignore is a directory")
            elif not os.path.isfile(expected_git_ignore_file):
                with open(expected_git_ignore_file, "w") as ign_file:
                    ign_file.write(app.config["PROJECT_ORCHEST_GIT_IGNORE_CONTENT"])

            # initialize with default environments only if the project has no environments directory
            expected_env_dir = os.path.join(
                full_project_path, ".orchest", "environments"
            )
            if os.path.isfile(expected_env_dir):
                raise NotADirectoryError(
                    "The expected environments directory (.orchest/environments) is a file."
                )
            elif not os.path.isdir(expected_env_dir):
                populate_default_environments(new_project.uuid)

            # refresh kernels after change in environments, given that  either we added the default environments
            # or the project has environments of its own
            populate_kernels(app, db)

            # build environments on project creation
            build_environments_for_project(new_project.uuid)

        # some calls rely on the project being in the db, like populate_default_environments or populate_kernels,
        # for this reason we need to commit the project to the db before the init actually finishes
        # if an exception is raised during project init we have to cleanup the newly added project from the db
        # TODO: make use of the complete cleanup of a project from orchest once that is implemented, so that we
        #  use the same code path
        except Exception as e:
            db.session.delete(new_project)
            db.session.commit()
            populate_kernels(app, db)
            raise e

    @app.route("/", methods=["GET"])
    def index():

        js_bundle_path = os.path.join(
            app.config["STATIC_DIR"], "js", "dist", "main.bundle.js"
        )
        css_bundle_path = os.path.join(app.config["STATIC_DIR"], "css", "main.css")

        return render_template(
            "index.html",
            javascript_bundle_hash=get_hash(js_bundle_path),
            css_bundle_hash=get_hash(css_bundle_path),
            user_config=get_user_conf(),
            DOCS_ROOT=app.config["DOCS_ROOT"],
            FLASK_ENV=app.config["FLASK_ENV"],
            SOCKETIO_NAMESPACE_ENV_BUILDS=_config.ORCHEST_SOCKETIO_ENV_BUILDING_NAMESPACE,
        )

    @app.route("/async/spawn-update-server", methods=["GET"])
    def spawn_update_server():

        client = docker.from_env()

        run_orchest_ctl(client, ["updateserver"])

        return ""

    @app.route("/heartbeat", methods=["GET"])
    def heartbeat():
        return ""

    @app.route("/async/restart", methods=["POST"])
    def restart_server():

        client = docker.from_env()

        if request.args.get("mode") == "dev":
            run_orchest_ctl(client, ["restart", "--mode=dev"])
        else:
            run_orchest_ctl(client, ["restart"])

        return ""

    @app.route("/async/version", methods=["GET"])
    def version():

        git_proc = subprocess.Popen(
            'echo "$(git describe --abbrev=0 --tags) "',
            cwd="/orchest-host",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        outs, _ = git_proc.communicate()

        return outs

    @app.route("/async/user-config", methods=["GET", "POST"])
    def user_config():

        if request.method == "POST":

            config = request.form.get("config")

            try:
                # only save if parseable JSON
                json.loads(config)
                save_user_conf_raw(config)

            except json.JSONDecodeError as e:
                logging.debug(e)

            return ""
        else:
            return get_user_conf_raw()

    @app.route(
        "/async/pipelines/delete/<project_uuid>/<pipeline_uuid>", methods=["DELETE"]
    )
    def pipelines_delete(project_uuid, pipeline_uuid):

        if (
            Pipeline.query.filter(Pipeline.uuid == pipeline_uuid)
            .filter(Pipeline.project_uuid == project_uuid)
            .count()
            > 0
        ):

            pipeline_json_path = get_pipeline_path(pipeline_uuid, project_uuid)
            os.remove(pipeline_json_path)

            pipeline = (
                Pipeline.query.filter(Pipeline.uuid == pipeline_uuid)
                .filter(Pipeline.project_uuid == project_uuid)
                .first()
            )
            db.session.delete(pipeline)
            db.session.commit()

            return jsonify({"success": True})
        else:
            return jsonify({"message": "Pipeline could not be found."}), 404

    @app.route("/async/experiments/create", methods=["POST"])
    def experiments_create():

        experiment_uuid = str(uuid.uuid4())

        new_ex = Experiment(
            uuid=experiment_uuid,
            name=request.json["name"],
            pipeline_uuid=request.json["pipeline_uuid"],
            project_uuid=request.json["project_uuid"],
            pipeline_name=request.json["pipeline_name"],
            strategy_json="",
            draft=True,
        )

        db.session.add(new_ex)
        db.session.commit()

        create_experiment_directory(
            experiment_uuid, request.json["pipeline_uuid"], request.json["project_uuid"]
        )

        return jsonify(experiment_schema.dump(new_ex))

    @app.route("/async/pipelineruns/create", methods=["POST"])
    def pipelineruns_create():

        experiment_uuid = request.json["experiment_uuid"]

        for idx, pipeline_run in enumerate(request.json["generated_pipeline_runs"]):
            pr = PipelineRun(
                uuid=request.json["experiment_json"]["pipeline_runs"][idx]["run_uuid"],
                experiment=experiment_uuid,
                parameter_json=pipeline_run,
                id=request.json["pipeline_run_ids"][idx],
            )

            db.session.add(pr)

        db.session.commit()

        return jsonify({"success": True})

    @app.route("/async/pipelines/create/<project_uuid>", methods=["POST"])
    def pipelines_create(project_uuid):

        pipeline_path = request.json["pipeline_path"]

        if (
            Pipeline.query.filter(Pipeline.project_uuid == project_uuid)
            .filter(Pipeline.path == pipeline_path)
            .count()
            == 0
        ):

            pipeline_uuid = str(uuid.uuid4())

            pipeline = Pipeline(
                path=pipeline_path, uuid=pipeline_uuid, project_uuid=project_uuid
            )
            db.session.add(pipeline)
            db.session.commit()

            pipeline_dir = get_pipeline_directory(pipeline_uuid, project_uuid)
            pipeline_json_path = get_pipeline_path(pipeline_uuid, project_uuid)

            os.makedirs(pipeline_dir, exist_ok=True)

            # generate clean pipeline.json
            pipeline_json = {
                "name": request.json["name"],
                "version": "1.0.0",
                "uuid": pipeline_uuid,
                "settings": {"auto_eviction": False},
                "steps": {},
            }

            with open(pipeline_json_path, "w") as pipeline_json_file:
                pipeline_json_file.write(json.dumps(pipeline_json, indent=2))

            return jsonify({"success": True})
        else:
            return (
                jsonify(
                    {"message": "Pipeline already exists at path '%s'." % pipeline_path}
                ),
                409,
            )

    class ImportGitProjectListResource(Resource):
        def post(self):
            n_uuid = str(uuid.uuid4())
            new_task = BackgroundTask(
                task_uuid=n_uuid, task_type="GIT_CLONE_PROJECT", status="PENDING"
            )
            db.session.add(new_task)
            db.session.commit()

            # start the background process in charge of cloning
            file_dir = os.path.dirname(os.path.realpath(__file__))
            args = [
                "python3",
                "-m",
                "scripts.background_tasks",
                "--type",
                "git_clone_project",
                "--uuid",
                n_uuid,
                "--url",
                request.json["url"],
            ]

            project_name = request.json.get("project_name", None)
            if project_name:
                args.append("--path")
                args.append(str(project_name))

            background_task_process = Popen(
                args,
                cwd=os.path.join(file_dir, "../.."),
                stderr=subprocess.STDOUT,
            )

            return background_task_schema.dump(new_task)

    api.add_resource(ImportGitProjectListResource, "/async/projects/import-git")

    @app.route("/async/projects", methods=["GET", "POST", "DELETE"])
    def projects():

        projects_dir = os.path.join(app.config["USER_DIR"], "projects")
        project_paths = [
            name
            for name in os.listdir(projects_dir)
            if os.path.isdir(os.path.join(projects_dir, name))
        ]

        # look for projects that have been removed through the filesystem by the user, cleanup
        # dangling resources
        fs_removed_projects = Project.query.filter(
            Project.path.notin_(project_paths)
        ).all()
        cleanup_fs_removed_projects(fs_removed_projects)

        # detect new projects by detecting directories that were not registered in the db as projects
        existing_project_paths = [
            project.path
            for project in Project.query.filter(Project.path.in_(project_paths)).all()
        ]
        new_project_paths = set(project_paths) - set(existing_project_paths)
        for new_project_path in new_project_paths:
            try:
                init_project(new_project_path)
            except Exception as e:
                logging.error(
                    f"Error during project initialization of {new_project_path}: {e}"
                )

        if request.method == "GET":

            projects = projects_schema.dump(Project.query.all())

            # Get counts for: pipelines, experiments and environments
            for project in projects:
                project["pipeline_count"] = Pipeline.query.filter(
                    Pipeline.project_uuid == project["uuid"]
                ).count()
                project["experiment_count"] = Experiment.query.filter(
                    Experiment.project_uuid == project["uuid"]
                ).count()
                project["environment_count"] = len(get_environments(project["uuid"]))

            return jsonify(projects)

        elif request.method == "DELETE":

            project_uuid = request.json["project_uuid"]

            project = Project.query.filter(Project.uuid == project_uuid).first()

            if project != None:

                project_path = project_uuid_to_path(project_uuid)
                full_project_path = os.path.join(projects_dir, project_path)

                # go through each environment and delete it
                proj_envs = get_environments(project_uuid)
                for env in proj_envs:
                    delete_environment(project_uuid, env.uuid)

                shutil.rmtree(full_project_path)

                db.session.delete(project)
                db.session.commit()

                # refresh kernels after change in environments
                populate_kernels(app, db)

                return jsonify({"message": "Project deleted."})
            else:
                return (
                    jsonify(
                        {"message": "Project not found for UUID %s." % project_uuid}
                    ),
                    404,
                )

        elif request.method == "POST":
            project_path = request.json["name"]

            if project_path not in project_paths:
                full_project_path = os.path.join(projects_dir, project_path)
                if not os.path.isdir(full_project_path):
                    os.makedirs(full_project_path)
                    # note that given the current pattern we have in the GUI, where we POST and then GET projects,
                    # this line does not strictly need to be there, since the new directory will be picked up
                    # on the GET request and initialized, placing it here is more explicit and less relying
                    # on the POST->GET pattern from the GUI
                    try:
                        init_project(project_path)
                    except Exception as e:
                        return (
                            jsonify(
                                {
                                    "message": "Failed to create the project. Error: %s"
                                    % e
                                }
                            ),
                            500,
                        )
                else:
                    return (
                        jsonify({"message": "Project directory already exists."}),
                        409,
                    )
            else:
                return (
                    jsonify({"message": "Project name already exists."}),
                    409,
                )

            return jsonify({"message": "Project created."})

    @app.route("/async/pipelines/<project_uuid>/<pipeline_uuid>", methods=["GET"])
    def pipeline_get(project_uuid, pipeline_uuid):

        pipeline = (
            Pipeline.query.filter(Pipeline.project_uuid == project_uuid)
            .filter(Pipeline.uuid == pipeline_uuid)
            .first()
        )

        if pipeline is None:
            return jsonify({"message": "Pipeline doesn't exist."}), 404
        else:
            return jsonify(pipeline_schema.dump(pipeline))

    @app.route("/async/pipelines/<project_uuid>", methods=["GET"])
    def pipelines_get(project_uuid):

        project_path = project_uuid_to_path(project_uuid)
        project_dir = os.path.join(app.config["USER_DIR"], "projects", project_path)

        if not os.path.isdir(project_dir):
            return jsonify({"message": "Project directory not found."}), 404

        # find all pipelines in project dir
        pipeline_paths = find_pipelines_in_dir(project_dir, project_dir)

        # identify all pipeline paths that are not yet a pipeline
        existing_pipeline_paths = [
            pipeline.path
            for pipeline in Pipeline.query.filter(Pipeline.path.in_(pipeline_paths))
            .filter(Pipeline.project_uuid == project_uuid)
            .all()
        ]

        # TODO: handle existing pipeline assignments
        new_pipeline_paths = set(pipeline_paths) - set(existing_pipeline_paths)

        for new_pipeline_path in new_pipeline_paths:

            # write pipeline uuid to file
            pipeline_json_path = get_pipeline_path(
                None, project_uuid, pipeline_path=new_pipeline_path
            )

            try:
                with open(pipeline_json_path, "r") as json_file:
                    pipeline_json = json.load(json_file)

                file_pipeline_uuid = pipeline_json.get("uuid")

                new_pipeline_uuid = file_pipeline_uuid

                # see if pipeline_uuid is taken
                if (
                    Pipeline.query.filter(Pipeline.uuid == file_pipeline_uuid)
                    .filter(Pipeline.project_uuid == project_uuid)
                    .count()
                    > 0
                    or len(file_pipeline_uuid) == 0
                ):
                    new_pipeline_uuid = str(uuid.uuid4())

                with open(pipeline_json_path, "w") as json_file:
                    pipeline_json["uuid"] = new_pipeline_uuid
                    json_file.write(json.dumps(pipeline_json, indent=2))

                # only commit if writing succeeds
                new_pipeline = Pipeline(
                    uuid=new_pipeline_uuid,
                    path=new_pipeline_path,
                    project_uuid=project_uuid,
                )
                db.session.add(new_pipeline)
                db.session.commit()

            except Exception as e:
                logging.info(e)

        pipelines = Pipeline.query.filter(Pipeline.project_uuid == project_uuid).all()
        pipelines_augmented = []

        for pipeline in pipelines:

            pipeline_json_path = get_pipeline_path(pipeline.uuid, pipeline.project_uuid)

            pipeline_augmented = {
                "uuid": pipeline.uuid,
                "path": pipeline.path,
            }
            if os.path.isfile(pipeline_json_path):
                with open(pipeline_json_path, "r") as json_file:
                    pipeline_json = json.load(json_file)
                    pipeline_augmented["name"] = pipeline_json["name"]
            else:
                pipeline_augmented["name"] = "Warning: pipeline file was not found."

            pipelines_augmented.append(pipeline_augmented)

        json_string = json.dumps({"success": True, "result": pipelines_augmented})

        return json_string, 200, {"content-type": "application/json"}

    @app.route(
        "/async/notebook_html/<project_uuid>/<pipeline_uuid>/<step_uuid>",
        methods=["GET"],
    )
    def notebook_html_get(project_uuid, pipeline_uuid, step_uuid):

        experiment_uuid = request.args.get("experiment_uuid")
        pipeline_run_uuid = request.args.get("pipeline_run_uuid")

        pipeline_json_path = get_pipeline_path(
            pipeline_uuid, project_uuid, experiment_uuid, pipeline_run_uuid
        )
        pipeline_dir = get_pipeline_directory(
            pipeline_uuid, project_uuid, experiment_uuid, pipeline_run_uuid
        )

        if os.path.isfile(pipeline_json_path):
            with open(pipeline_json_path, "r") as json_file:
                pipeline_json = json.load(json_file)

            try:
                notebook_path = os.path.join(
                    pipeline_dir, pipeline_json["steps"][step_uuid]["file_path"]
                )
            except Exception as e:
                logging.info(e)
                return return_404(
                    "Invalid JSON for pipeline %s error: %e" % (pipeline_json_path, e)
                )
        else:
            return return_404(
                "Could not find pipeline.json for pipeline %s" % pipeline_json_path
            )

        if os.path.isfile(notebook_path):
            try:

                html_exporter = HTMLExporter()
                (body, _) = html_exporter.from_filename(notebook_path)

                return body

            except IOError as error:
                logging.info(
                    "Error opening notebook file %s error: %s" % (notebook_path, error)
                )
                return return_404("Could not find notebook file %s" % notebook_path)

    @app.route(
        "/async/pipelines/json/<project_uuid>/<pipeline_uuid>", methods=["GET", "POST"]
    )
    def pipelines_json(project_uuid, pipeline_uuid):

        pipeline_json_path = get_pipeline_path(
            pipeline_uuid,
            project_uuid,
            request.args.get("experiment_uuid"),
            request.args.get("pipeline_run_uuid"),
        )

        if request.method == "POST":

            pipeline_directory = get_pipeline_directory(
                pipeline_uuid,
                project_uuid,
                request.args.get("experiment_uuid"),
                request.args.get("pipeline_run_uuid"),
            )

            # parse JSON
            pipeline_json = json.loads(request.form.get("pipeline_json"))

            # first create all files part of pipeline_json definition
            # TODO: consider removing other files (no way to do this reliably,
            # special case might be rename)
            create_pipeline_files(pipeline_json, pipeline_directory, project_uuid)

            # side effect: for each Notebook in de pipeline.json set the correct kernel
            pipeline_set_notebook_kernels(
                pipeline_json, pipeline_directory, project_uuid
            )

            with open(pipeline_json_path, "w") as json_file:
                json_file.write(json.dumps(pipeline_json, indent=2))

            return jsonify({"message": "Successfully saved pipeline."})

        elif request.method == "GET":

            if not os.path.isfile(pipeline_json_path):
                return (
                    jsonify(
                        {
                            "success": False,
                            "reason": ".orchest file doesn't exist at location %s"
                            % pipeline_json_path,
                        }
                    ),
                    404,
                )
            else:
                with open(pipeline_json_path) as json_file:
                    return jsonify({"success": True, "pipeline_json": json_file.read()})

            return ""

    @app.route(
        "/async/file-picker-tree/pipeline-cwd/<project_uuid>/<pipeline_uuid>",
        methods=["GET"],
    )
    def pipeline_cwd(project_uuid, pipeline_uuid):

        pipeline_dir = get_pipeline_directory(pipeline_uuid, project_uuid)
        project_dir = get_project_directory(project_uuid)
        cwd = pipeline_dir.replace(project_dir, "")

        return jsonify({"cwd": cwd})

    @app.route("/async/file-picker-tree/<project_uuid>", methods=["GET"])
    def get_file_picker_tree(project_uuid):

        allowed_file_extensions = ["ipynb", "R", "py", "sh"]

        project_dir = get_project_directory(project_uuid)

        if not os.path.isdir(project_dir):
            return jsonify({"message": "Project dir %s not found." % project_dir}), 404

        tree = {"type": "directory", "root": True, "name": "/", "children": []}

        dir_nodes = {}

        dir_nodes[project_dir] = tree

        for root, dirs, files in os.walk(project_dir):

            # exclude directories that start with "." from file_picker
            dirs[:] = [dirname for dirname in dirs if not dirname.startswith(".")]

            for dirname in dirs:

                dir_path = os.path.join(root, dirname)
                dir_node = {
                    "type": "directory",
                    "name": dirname,
                    "children": [],
                }

                dir_nodes[dir_path] = dir_node
                dir_nodes[root]["children"].append(dir_node)

            for filename in files:

                if filename.split(".")[-1] in allowed_file_extensions:
                    file_node = {
                        "type": "file",
                        "name": filename,
                    }

                    # this key should always exist
                    try:
                        dir_nodes[root]["children"].append(file_node)
                    except KeyError as e:
                        logging.error(
                            "Key %s does not exist in dir_nodes %s. Error: %s"
                            % (root, dir_nodes, e)
                        )
                    except Exception as e:
                        logging.error("Error: %e" % e)

        return jsonify(tree)

    @app.route("/async/project-files/create/<project_uuid>", methods=["POST"])
    def create_project_file(project_uuid):
        """Create project file in specified directory within project."""

        project_dir = get_project_directory(project_uuid)

        # Client sends absolute path relative to project root, hence starting /
        # is removed.
        file_path = os.path.join(project_dir, request.json["file_path"][1:])

        if os.path.isfile(file_path):
            return jsonify({"message": "File already exists."}), 409

        try:
            open(file_path, "a").close()
            return jsonify({"message": "File created."})
        except IOError as e:
            logging.error("Could not create file at %s. Error: %s" % (file_path, e))

    @app.route(
        "/async/project-files/exists/<project_uuid>/<pipeline_uuid>", methods=["POST"]
    )
    def project_file_exists(project_uuid, pipeline_uuid):
        """Check whether file exists"""

        pipeline_dir = get_pipeline_directory(pipeline_uuid, project_uuid)
        file_path = os.path.join(pipeline_dir, request.json["relative_path"])

        if os.path.isfile(file_path):
            return jsonify({"message": "File exists."})
        else:
            return jsonify({"message": "File does not exists."}), 404
