import abc
import collections
import getpass
import math
import os
import subprocess
import sys
import textwrap
import time
import warnings
from distutils.spawn import find_executable
from typing import List

import adaptive_scheduler._mock_scheduler
from adaptive_scheduler.utils import _progress


class BaseScheduler(metaclass=abc.ABCMeta):
    def __init__(
        self,
        cores,
        run_script,
        python_executable,
        log_folder,
        mpiexec_executable,
        executor_type,
        num_threads,
        extra_scheduler,
        extra_env_vars,
    ):
        self.cores = cores
        self.run_script = run_script
        self.python_executable = python_executable or sys.executable
        self.log_folder = log_folder
        self.mpiexec_executable = mpiexec_executable or "mpiexec"
        self.executor_type = executor_type
        self.num_threads = num_threads
        self._extra_scheduler = extra_scheduler
        self._extra_env_vars = extra_env_vars

    @abc.abstractmethod
    def queue(self, me_only):
        """Get the current running and pending jobs.

        Parameters
        ----------
        me_only : bool, default: True
            Only see your jobs.

        Returns
        -------
        dictionary of `job_id` -> dict with `name` and `state`, for
        example ``{job_id: {"job_name": "TEST_JOB-1", "state": "R" or "Q"}}``.

        Notes
        -----
        This function might return extra information about the job, however
        this is not used elsewhere in this package.
        """
        pass

    @property
    def ext(self):
        return self._ext

    @property
    def submit_cmd(self):
        return self._submit_cmd

    @abc.abstractmethod
    def job_script(self):
        pass

    def batch_fname(self, name):
        return name + self.ext

    def cancel(
        self, job_names: List[str], with_progress_bar: bool = True, max_tries: int = 5
    ) -> None:
        """Cancel all jobs in `job_names`.

        Parameters
        ----------
        job_names : list
            List of job names.
        with_progress_bar : bool, default: True
            Display a progress bar using `tqdm`.
        max_tries : int, default: 5
            Maximum number of attempts to cancel a job.
        """

        def to_cancel(job_names):
            return [
                job_id
                for job_id, info in self.queue().items()
                if info["job_name"] in job_names
            ]

        def cancel_jobs(job_ids):
            for job_id in _progress(job_ids, with_progress_bar, "Canceling jobs"):
                cmd = f"{self._cancel_cmd} {job_id}".split()
                returncode = subprocess.run(cmd, stderr=subprocess.PIPE).returncode
                if returncode != 0:
                    warnings.warn(f"Couldn't cancel '{job_id}'.", UserWarning)

        job_names = set(job_names)
        for _ in range(max_tries):
            job_ids = to_cancel(job_names)
            if not job_ids:
                # no more running jobs
                break
            cancel_jobs(job_ids)
            time.sleep(0.5)

    def _mpi4py(self, name):
        log_fname = self.log_fname(name)
        return f"{self.mpiexec_executable} -n {self.cores} {self.python_executable} -m mpi4py.futures {self.run_script} --log-fname {log_fname} --job-id {self._JOB_ID_VARIABLE} --name {name}"

    def _dask_mpi(self, name):
        log_fname = self.log_fname(name)
        return f"{self.mpiexec_executable} -n {self.cores} {self.python_executable} {self.run_script} --log-fname {log_fname} --job-id {self._JOB_ID_VARIABLE} --name {name}"

    def _ipyparallel(self, name):
        log_fname = self.log_fname(name)
        job_id = self._JOB_ID_VARIABLE
        profile = "${profile}"
        return textwrap.dedent(
            f"""\
            profile=adaptive_scheduler_{job_id}

            echo "Creating profile {profile}"
            ipython profile create {profile}

            echo "Launching controller"
            ipcontroller --ip="*" --profile={profile} --log-to-file &
            sleep 10

            echo "Launching engines"
            {self.mpiexec_executable} -n {self.cores-1} ipengine --profile={profile} --mpi --cluster-id='' --log-to-file &

            echo "Starting the Python script"
            {self.python_executable} {self.run_script} --profile {profile} --n {self.cores-1} --log-fname {log_fname} --job-id {job_id} --name {name}
            """
        )

    def _executor_specific(self, name):
        if self.executor_type == "mpi4py":
            return self._mpi4py(name)
        elif self.executor_type == "dask-mpi":
            return self._dask_mpi(name)
        elif self.executor_type == "ipyparallel":
            if self.cores <= 1:
                raise ValueError(
                    "`ipyparalllel` uses 1 cores of the `adaptive.Runner` and"
                    "the rest of the cores for the engines, so use more than 1 core."
                )
            return self._ipyparallel(name)
        else:
            raise NotImplementedError("Use 'ipyparallel', 'dask-mpi' or 'mpi4py'.")

    def log_fname(self, name):
        if self.log_folder:
            os.makedirs(self.log_folder, exist_ok=True)
        return os.path.join(self.log_folder, f"{name}-{self._JOB_ID_VARIABLE}.log")

    def output_fnames(self, name):
        log_fname = self.log_fname(name)
        return [log_fname.replace(".log", ".out")]

    @property
    def extra_scheduler(self):
        extra_scheduler = self._extra_scheduler or []
        return "\n".join(f"#{self._scheduler} {arg}" for arg in extra_scheduler)

    @property
    def extra_env_vars(self):
        extra_env_vars = self._extra_env_vars or []
        return "\n".join(f"export {arg}" for arg in extra_env_vars)

    def write_job_script(self, name):
        with open(self.batch_fname(name), "w") as f:
            job_script = self.job_script(name)
            f.write(job_script)

    def start_job(self, name):
        self.write_job_script(name)
        returncode = None
        while returncode != 0:
            returncode = subprocess.run(
                f"{self.submit_cmd} {name}{self.ext}".split(), stderr=subprocess.PIPE
            ).returncode
            time.sleep(0.5)


class PBS(BaseScheduler):
    def __init__(
        self,
        cores,
        run_script="run_learner.py",
        python_executable=None,
        log_folder="",
        mpiexec_executable=None,
        executor_type="mpi4py",
        num_threads=1,
        extra_scheduler=None,
        extra_env_vars=None,
        *,
        cores_per_node=None,
    ):
        super().__init__(
            cores,
            run_script,
            python_executable,
            log_folder,
            mpiexec_executable,
            executor_type,
            num_threads,
            extra_scheduler,
            extra_env_vars,
        )
        # Attributes that all schedulers need to have
        self._ext = ".batch"
        # the "-k oe" flags with "qsub" writes the log output to
        # files directly instead of at the end of the job. The downside
        # is that the logfiles are put in the homefolder.
        self._submit_cmd = "qsub -k oe"
        self._JOB_ID_VARIABLE = "${PBS_JOBID}"
        self._scheduler = "PBS"
        self._cancel_cmd = "qdel"

        # PBS specific
        self.cores_per_node = cores_per_node
        self._calculate_nnodes()
        if cores != self.cores:
            warnings.warn(f"`self.cores` changed from {cores} to {self.cores}")

    def _calculate_nnodes(self):
        if self.cores_per_node is None:
            partial_msg = "Use set `cores_per_node=...` before passing the scheduler."
            try:
                max_cores_per_node = self._guess_cores_per_node()
                self.nnodes = math.ceil(self.cores / max_cores_per_node)
                self.cores_per_node = round(self.cores / self.nnodes)
                msg = (
                    f"`#PBS -l nodes={self.nnodes}:ppn={self.cores_per_node}` is"
                    f" guessed using the `qnodes` command, we set"
                    f" `cores_per_node={self.cores_per_node}`."
                    f" You might want to change this. {partial_msg}"
                )
                warnings.warn(msg)
                self.cores = self.nnodes * self.cores_per_node
            except Exception as e:
                msg = (
                    f"Got an error: {e}."
                    " Couldn't guess `cores_per_node`, this argument is required"
                    f" for PBS. {partial_msg}"
                    " We set `cores_per_node=1`!"
                )
                warnings.warn(msg)
                self.nnodes = self.cores
                self.cores_per_nodes = 1
        else:
            self.nnodes = self.cores / self.cores_per_node
            if not float(self.nnodes).is_integer():
                raise ValueError("cores / cores_per_node must be an integer!")
            else:
                self.nnodes = int(self.nnodes)

    def output_fnames(self, name):
        """The "-k oe" flags with "qsub" writes the log output to
        files directly instead of at the end of the job. The downside
        is that the logfiles are put in the homefolder."""
        home = os.path.expanduser("~/")
        stdout, stderr = [
            os.path.join(home, f"{name}.{x}{self._JOB_ID_VARIABLE}") for x in "oe"
        ]
        return [stdout, stderr]

    def job_script(self, name):
        """Get a jobscript in string form.

        Returns
        -------
        job_script : str
            A job script that can be submitted to PBS.
        """

        job_script = textwrap.dedent(
            f"""\
            #!/bin/sh
            #PBS -l nodes={self.nnodes}:ppn={self.cores_per_node}
            #PBS -V
            #PBS -N {name}
            #PBS -o /tmp/placeholder
            #PBS -e /tmp/placeholder
            {{extra_scheduler}}

            export MKL_NUM_THREADS={self.num_threads}
            export OPENBLAS_NUM_THREADS={self.num_threads}
            export OMP_NUM_THREADS={self.num_threads}
            {{extra_env_vars}}

            cd $PBS_O_WORKDIR

            {{executor_specific}}
            """
        )

        job_script = job_script.format(
            extra_scheduler=self.extra_scheduler,
            extra_env_vars=self.extra_env_vars,
            executor_specific=self._executor_specific(name),
            job_id_variable=self._JOB_ID_VARIABLE,
        )

        return job_script

    @staticmethod
    def _split_by_job(lines):
        jobs = [[]]
        for line in lines:
            line = line.strip()
            if line:
                jobs[-1].append(line)
            else:
                jobs.append([])
        return [j for j in jobs if j]

    @staticmethod
    def _fix_line_cuts(raw_info):
        info = []
        for line in raw_info:
            if " = " in line:
                info.append(line)
            else:
                info[-1] += line
        return info

    def queue(self, me_only=True):
        """Get the current running and pending jobs.

        Parameters
        ----------
        me_only : bool, default: True
            Only see your jobs.

        Returns
        -------
        dictionary of `job_id` -> dict with `name` and `state`, for
        example ``{job_id: {"job_name": "TEST_JOB-1", "state": "R" or "Q"}}``.

        Notes
        -----
        This function returns extra information about the job, however this is not
        used elsewhere in this package.
        """
        cmd = ["qstat", "-f"]

        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            env=dict(os.environ, SGE_LONG_QNAMES="1000"),
        )
        output = proc.stdout

        if proc.returncode != 0:
            raise RuntimeError("qstat is not responding.")

        jobs = self._split_by_job(output.replace("\n\t", "").split("\n"))

        running = {}
        for header, *raw_info in jobs:
            job_id = header.split("Job Id: ")[1]
            info = dict([line.split(" = ") for line in self._fix_line_cuts(raw_info)])
            if info["job_state"] in ["R", "Q"]:
                info["job_name"] = info[
                    "Job_Name"
                ]  # used in `server_support.manage_jobs`
                info["state"] = info["job_state"]  # used in `RunManager.live`
                running[job_id] = info

        if me_only:
            # We do this because the "-u [username here]"  flag doesn't
            # work with "-f" on some clusters.
            username = getpass.getuser()
            running = {
                job_id: info
                for job_id, info in running.items()
                if username in info["Job_Owner"]
            }

        return running

    def _qnodes(self):
        proc = subprocess.run(["qnodes"], text=True, capture_output=True)
        output = proc.stdout

        if proc.returncode != 0:
            raise RuntimeError("qnodes is not responding.")

        jobs = self._split_by_job(output.replace("\n\t", "").split("\n"))

        nodes = {
            node: dict([line.split(" = ") for line in self._fix_line_cuts(raw_info)])
            for node, *raw_info in jobs
        }
        return nodes

    def _guess_cores_per_node(self):
        nodes = self._qnodes()
        cntr = collections.Counter([int(info["np"]) for info in nodes.values()])
        ncores, freq = cntr.most_common(1)[0]
        return ncores


class SLURM(BaseScheduler):
    def __init__(
        self,
        cores,
        run_script="run_learner.py",
        python_executable=None,
        log_folder="",
        mpiexec_executable=None,
        executor_type="mpi4py",
        num_threads=1,
        extra_scheduler=None,
        extra_env_vars=None,
    ):
        super().__init__(
            cores,
            run_script,
            python_executable,
            log_folder,
            mpiexec_executable,
            executor_type,
            num_threads,
            extra_scheduler,
            extra_env_vars,
        )
        # Attributes that all schedulers need to have
        self._ext = ".sbatch"
        self._submit_cmd = "sbatch"
        self._JOB_ID_VARIABLE = "${SLURM_JOB_ID}"
        self._scheduler = "SLURM"
        self._cancel_cmd = "scancel"

        # SLURM specific
        self.mpiexec_executable = mpiexec_executable or "srun --mpi=pmi2"

    def _ipyparallel(self, name):
        log_fname = self.log_fname(name)
        job_id = self._JOB_ID_VARIABLE
        profile = "${profile}"
        return textwrap.dedent(
            f"""\
            profile=adaptive_scheduler_{job_id}

            echo "Creating profile {profile}"
            ipython profile create {profile}

            echo "Launching controller"
            ipcontroller --ip="*" --profile={profile} --log-to-file &
            sleep 10

            echo "Launching engines"
            srun --ntasks {self.cores-1} ipengine --profile={profile} --cluster-id='' --log-to-file &

            echo "Starting the Python script"
            srun --ntasks 1 {self.python_executable} {self.run_script} --profile {profile} --n {self.cores-1} --log-fname {log_fname} --job-id {job_id} --name {name}
            """
        )

    def job_script(self, name):
        """Get a jobscript in string form.

        Returns
        -------
        job_script : str
            A job script that can be submitted to SLURM.
        """
        output_fname = self.output_fnames(name).replace(self._JOB_ID_VARIABLE, "%A")
        job_script = textwrap.dedent(
            f"""\
            #!/bin/bash
            #SBATCH --job-name {name}
            #SBATCH --ntasks {self.cores}
            #SBATCH --no-requeue
            #SBATCH --output {output_fname}
            {{extra_scheduler}}

            export MKL_NUM_THREADS={self.num_threads}
            export OPENBLAS_NUM_THREADS={self.num_threads}
            export OMP_NUM_THREADS={self.num_threads}
            {{extra_env_vars}}

            {{executor_specific}}
            """
        )

        job_script = job_script.format(
            extra_scheduler=self.extra_scheduler,
            extra_env_vars=self.extra_env_vars,
            executor_specific=self._executor_specific(name),
        )
        return job_script

    def queue(self, me_only=True):
        """Get the current running and pending jobs.

        Parameters
        ----------
        me_only : bool, default: True
            Only see your jobs.

        Returns
        -------
        dictionary of `job_id` -> dict with `name` and `state`, for
        example ``{job_id: {"job_name": "TEST_JOB-1", "state": "RUNNING" or "PENDING"}}``.

        Notes
        -----
        This function returns extra information about the job, however this is not
        used elsewhere in this package.
        """
        python_format = {
            "jobid": 100,
            "name": 100,
            "state": 100,
            "numnodes": 100,
            "reasonlist": 4000,
        }  # (key -> length) mapping

        slurm_format = ",".join(f"{k}:{v}" for k, v in python_format.items())
        cmd = [
            "/usr/bin/squeue",
            rf'--Format=",{slurm_format},"',
            "--noheader",
            "--array",
        ]
        if me_only:
            username = getpass.getuser()
            cmd.append(f"--user={username}")
        proc = subprocess.run(cmd, text=True, capture_output=True)
        output = proc.stdout

        if (
            "squeue: error" in output
            or "slurm_load_jobs error" in output
            or proc.returncode != 0
        ):
            raise RuntimeError("SLURM is not responding.")

        def line_to_dict(line):
            line = list(line)
            info = {}
            for k, v in python_format.items():
                info[k] = "".join(line[:v]).strip()
                line = line[v:]
            return info

        squeue = [line_to_dict(line) for line in output.split("\n")]
        squeue = [info for info in squeue if info["state"] in ("PENDING", "RUNNING")]
        running = {info.pop("jobid"): info for info in squeue}
        for info in running.values():
            info["job_name"] = info.pop("name")
        return running


class LocalMockScheduler(BaseScheduler):
    """A scheduler that can be used for testing and runs locally.

    DOESN'T WORK ATM!!!
    """

    def __init__(
        self,
        cores,
        run_script="run_learner.py",
        python_executable=None,
        log_folder="",
        mpiexec_executable=None,
        executor_type="mpi4py",
        num_threads=1,
        extra_scheduler=None,
        extra_env_vars=None,
    ):
        warnings.warn("The LocalMockScheduler currently doesn't work!")
        super().__init__(
            cores,
            run_script,
            python_executable,
            log_folder,
            mpiexec_executable,
            executor_type,
            num_threads,
            extra_scheduler,
            extra_env_vars,
        )
        # LocalMockScheduler specific
        self.mock_scheduler = adaptive_scheduler._mock_scheduler.MockScheduler()
        mock_scheduler_file = adaptive_scheduler._mock_scheduler.__file__
        self.base_cmd = f"{self.python_executable} {mock_scheduler_file}"

        # Attributes that all schedulers need to have
        self._ext = ".batch"
        self._submit_cmd = f"{self.base_cmd} --submit"
        self._JOB_ID_VARIABLE = "${JOB_ID}"
        self._cancel_cmd = f"{self.base_cmd} --cancel"

    def job_script(self, name):
        """Get a jobscript in string form.

        Returns
        -------
        job_script : str
            A job script that can be submitted to PBS.
        """

        job_script = textwrap.dedent(
            f"""\
            #!/bin/sh

            export MKL_NUM_THREADS={self.num_threads}
            export OPENBLAS_NUM_THREADS={self.num_threads}
            export OMP_NUM_THREADS={self.num_threads}
            {{extra_env_vars}}

            cd $PBS_O_WORKDIR

            {{executor_specific}}
            """
        )

        job_script = job_script.format(
            extra_env_vars=self.extra_env_vars,
            executor_specific=self._executor_specific(name),
            job_id_variable=self._JOB_ID_VARIABLE,
        )

        return job_script

    def queue(self, me_only=True):
        """Get the current running and pending jobs.

        Parameters
        ----------
        me_only : bool, default: True
            Only see your jobs.

        Returns
        -------
        dictionary of `job_id` -> dict with `name` and `state`, for
        example ``{job_id: {"job_name": "TEST_JOB-1", "state": "R" or "Q"}}``.

        Notes
        -----
        This function returns extra information about the job, however this is not
        used elsewhere in this package.
        """
        return self.mock_scheduler.queue()

    def start_job(self, name):
        self.write_job_script(name)
        self.mock_scheduler.submit(name, self.batch_fname(name))

    @property
    def extra_scheduler(self):
        raise NotImplementedError("extra_scheduler is not implemented.")


def _get_default_scheduler():
    """Determine which scheduler system is being used.

    It tries to determine it by running both PBS and SLURM commands.

    If both are available then one needs to set an environment variable
    called 'SCHEDULER_SYSTEM' which is either 'PBS' or 'SLURM'.

    For example add the following to your `.bashrc`

    ```bash
    export SCHEDULER_SYSTEM="PBS"
    ```

    By default it is "SLURM".
    """

    has_pbs = bool(find_executable("qsub")) and bool(find_executable("qstat"))
    has_slurm = bool(find_executable("sbatch")) and bool(find_executable("squeue"))

    DEFAULT = SLURM
    default_msg = f"We set DefaultScheduler to '{DEFAULT}'."
    scheduler_system = os.environ.get("SCHEDULER_SYSTEM", "").upper()
    if scheduler_system:
        if scheduler_system not in ("PBS", "SLURM"):
            warnings.warn(
                f"SCHEDULER_SYSTEM={scheduler_system} is not implemented."
                f"Use SLURM or PBS. {default_msg}"
            )
            return DEFAULT
        else:
            return {"SLURM": SLURM, "PBS": PBS}[scheduler_system]
    elif has_slurm and has_pbs:
        msg = f"Both SLURM and PBS are detected. {default_msg}"
        warnings.warn(msg)
        return DEFAULT
    elif has_pbs:
        return PBS
    elif has_slurm:
        return SLURM
    else:
        msg = f"No scheduler system could be detected. {default_msg}"
        warnings.warn(msg)
        return DEFAULT


DefaultScheduler = _get_default_scheduler()
