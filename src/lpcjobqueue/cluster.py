import os
import logging
import asyncio
import weakref
import random
import socket
import sys
import yaml
import dask
from distributed.core import Status
from dask_jobqueue.htcondor import (
    HTCondorCluster,
    HTCondorJob,
    quote_arguments,
    quote_environment,
)
from .schedd import htcondor, SCHEDD


logger = logging.getLogger(__name__)
fn = os.path.join(os.path.dirname(__file__), "config.yaml")
dask.config.ensure_file(source=fn)

with open(fn) as f:
    defaults = yaml.safe_load(f)

dask.config.update(dask.config.config, defaults, priority="old")


def is_venv():
    return hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )


class LPCCondorJob(HTCondorJob):
    executable = "/usr/bin/env"
    container_prefix = "/cvmfs/unpacked.cern.ch/registry.hub.docker.com/"
    config_name = "lpccondor"
    known_jobs = set()

    def __init__(
        self,
        scheduler=None,
        name=None,
        *,
        image="coffeateam/coffea-dask:latest",
        **base_class_kwargs,
    ):
        image = self.container_prefix + image
        base_class_kwargs["python"] = "python"
        super().__init__(scheduler=scheduler, name=name, **base_class_kwargs)
        homedir = os.path.expanduser("~")
        if self.log_directory:
            if not self.log_directory.startswith(homedir):
                raise ValueError(
                    f"log_directory must be a subpath of {homedir} or else the schedd cannot write our logs back to the container"
                )

        self.job_header_dict.update(
            {
                "initialdir": homedir,
                "use_x509userproxy": "true",
                "when_to_transfer_output": "ON_EXIT_OR_EVICT",
                "transfer_output_files": "",
                "+SingularityImage": f'"{image}"',
            }
        )

    def job_script(self):
        """ Construct a job submission script """
        quoted_arguments = quote_arguments(self._command_template.split(" "))
        quoted_environment = quote_environment(self.env_dict)
        job_header_lines = "\n".join(
            "%s = %s" % (k, v) for k, v in self.job_header_dict.items()
        )
        return self._script_template % {
            "shebang": self.shebang,
            "job_header": job_header_lines,
            "quoted_environment": quoted_environment,
            "quoted_arguments": quoted_arguments,
            "executable": self.executable,
        }

    async def start(self):
        """ Start workers and point them to our local scheduler """
        logger.info("Starting worker: %s", self.name)

        job = self.job_script()
        job = htcondor.Submit(job)

        def sub():
            try:
                classads = []
                with SCHEDD.transaction() as txn:
                    cluster_id = job.queue(txn, ad_results=classads)

                logger.debug(f"ClassAds for job {cluster_id}: {classads}")
                SCHEDD.spool(classads)
                return cluster_id
            except htcondor.HTCondorInternalError as ex:
                logger.error(str(ex))
                return None

        self.job_id = await asyncio.get_event_loop().run_in_executor(None, sub)
        if self.job_id:
            self.known_jobs.add(self.job_id)
            weakref.finalize(self, self._close_job, self.job_id)

            logger.info("Starting job: %s", self.job_id)
            # dask_jobqueue Job class does some things we don't want
            # so we do what is done in distributed.ProcessInterface
            self.status = Status.running

    async def close(self):
        logger.info(f"Closing worker {self.name} job_id {self.job_id} (current status: {self.status})")
        if self._cluster:
            # workaround for https://github.com/dask/distributed/issues/4532
            ret = await self._cluster().scheduler_comm.retire_workers(names=[self.name], remove=True, close_workers=True)
            # adaptive cluster scaling seems to call this properly already, so may be a no-op
            logger.debug(f"Worker {self.name} retirement info: {ret}")

        def check_gone():
            return len(SCHEDD.query(f"ClusterId == {self.job_id}")) == 0

        for _ in range(30):
            await asyncio.sleep(1)
            if await asyncio.get_event_loop().run_in_executor(None, check_gone):
                logger.info(f"Gracefully closed job {self.job_id}")
                self.known_jobs.discard(self.job_id)
                self.status = Status.closed
                self._event_finished.set()
                return

        logger.info(
            "Reached timeout, forcefully stopping worker: %s job: %s",
            self.name,
            self.job_id,
        )

        def stop():
            res = SCHEDD.act(htcondor.JobAction.Remove, f"ClusterId == {self.job_id}")
            if res["TotalSuccess"] == 1 and res["TotalChangedAds"] == 1:
                return True
            return False

        result = await asyncio.get_event_loop().run_in_executor(None, stop)
        if result:
            logger.info(f"Forcefully closed job {self.job_id}")
            self.known_jobs.discard(self.job_id)
            self.status = Status.closed
            self._event_finished.set()
            return
        logger.error(f"Failed to forcefully close job {self.job_id}")

    @classmethod
    def _close_job(cls, job_id):
        if job_id in cls.known_jobs:
            logger.warning(f"Last-ditch attempt to close HTCondor job {job_id} in finalizer! You should confirm the job exits!")
            SCHEDD.act(htcondor.JobAction.Remove, f"ClusterId == {job_id}")
            cls.known_jobs.discard(job_id)


class LPCCondorCluster(HTCondorCluster):
    __doc__ = (
        HTCondorCluster.__doc__
        + """
        More LPC-specific info...
    """
    )
    job_cls = LPCCondorJob
    config_name = "lpccondor"

    def __init__(self, **kwargs):
        hostname = socket.gethostname()
        port = random.randint(10000, 10100)
        scheduler_options = {"host": f"{hostname}:{port}"}
        if "scheduler_options" in kwargs:
            kwargs["scheduler_options"].setdefault(scheduler_options)
        else:
            kwargs["scheduler_options"] = scheduler_options
        try:
            super().__init__(**kwargs)
        except OSError:
            raise RuntimeError(
                f"Likely failed to bind to local port {port}, try rerunning"
            )
