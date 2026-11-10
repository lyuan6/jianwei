"""晨报后台任务：两个工作线程，逐步保存工作底稿。"""

from concurrent.futures import ThreadPoolExecutor
from functools import partial

from src.brief import get_or_generate
from src.research import ask
from src import timeline


class JobManager:
    def __init__(self, store, settings, generator=get_or_generate, researcher=None,
                 initializer=timeline.initialize, linker=timeline.analyze_link):
        self.store = store
        self.settings = settings
        self.generator = generator
        self.researcher = researcher or partial(ask, store=store)
        self.initializer = initializer
        self.linker = linker
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jianwei")

    def submit(self, username, codes, day, limit):
        inputs = {"codes": sorted(set(codes)), "date": day}
        job_id, created = self.store.create_job(username, "brief", inputs, day, limit)
        if created:
            self.executor.submit(self._run, job_id, inputs)
        return job_id

    def _run(self, job_id, inputs):
        self.store.update_job(job_id, status="running")
        progress = lambda steps: self.store.update_job(job_id, steps=steps)
        try:
            kind = inputs.get("kind")
            if kind == "research":
                result = self.researcher(inputs["question"], self.settings, inputs["holdings"], on_step=progress)
            elif kind == "timeline":
                result = self.initializer(inputs["code"], self.settings, self.store, on_step=progress)
            elif kind == "link":
                result = self.linker(inputs["username"], inputs["node_a"], inputs["node_b"], self.settings, self.store, on_step=progress)
            else:
                result = self.generator(inputs["codes"], self.settings, self.store, inputs["date"], on_step=progress)
            self.store.update_job(job_id, status="done", result=result, steps=result["steps"])
        except Exception as exc:
            error = str(exc) if isinstance(exc, ValueError) else "任务失败，请稍后重试。"
            self.store.update_job(job_id, status="failed", error=error)

    def submit_research(self, username, question, holdings, day, limit):
        inputs = {"kind": "research", "question": question, "holdings": holdings, "date": day}
        job_id, created = self.store.create_job(username, "research", inputs, day, limit)
        if created:
            self.executor.submit(self._run, job_id, inputs)
        return job_id

    def submit_timeline(self, username, code, day, limit):
        inputs = {"kind": "timeline", "code": code}
        job_id, created = self.store.create_job(username, "timeline", inputs, day, limit)
        if created:
            self.executor.submit(self._run, job_id, inputs)
        return job_id

    def submit_link(self, username, node_a, node_b, day, limit):
        inputs = {"kind": "link", "username": username, "node_a": node_a, "node_b": node_b}
        job_id, created = self.store.create_job(username, "link", inputs, day, limit)
        if created:
            self.executor.submit(self._run, job_id, inputs)
        return job_id

    def close(self):
        self.executor.shutdown(wait=True)
