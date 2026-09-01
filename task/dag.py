from collections import defaultdict, deque
from typing import Dict, List, Set
from task.schema import TaskDefinition


class DAGCycleError(Exception):
    pass


class DAGDependencyError(Exception):
    pass


class TaskDAG:
    def __init__(self):
        self.tasks: Dict[str, TaskDefinition] = {}
        self.adj_list: Dict[str, List[str]] = defaultdict(list)
        self.in_degree: Dict[str, int] = defaultdict(int)

    def add_task(self, task: TaskDefinition):
        self.tasks[task.task_id] = task

    def build_and_validate(self):
        self.adj_list.clear()
        self.in_degree.clear()

        # Initialize in-degrees for all tasks
        for task_id in self.tasks:
            self.in_degree[task_id] = 0

        # Build graph and validate dependencies exist
        for task_id, task in self.tasks.items():
            for dep_id in task.dependencies:
                if dep_id not in self.tasks:
                    raise DAGDependencyError(
                        f"Task '{task_id}' depends on non-existent task '{dep_id}'"
                    )
                self.adj_list[dep_id].append(task_id)
                self.in_degree[task_id] += 1

        # Check for cycles using Kahn's algorithm
        visited_count = 0
        queue = deque([tid for tid, deg in self.in_degree.items() if deg == 0])

        while queue:
            curr = queue.popleft()
            visited_count += 1
            for neighbor in self.adj_list[curr]:
                self.in_degree[neighbor] -= 1
                if self.in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(self.tasks):
            raise DAGCycleError("Cyclic dependency detected in Task DAG.")

        # Re-initialize in-degree for runtime scheduling
        self.in_degree.clear()
        for task_id in self.tasks:
            self.in_degree[task_id] = len(self.tasks[task_id].dependencies)

    def get_ready_tasks(self, completed_task_ids: Set[str], active_task_ids: Set[str]) -> List[TaskDefinition]:
        ready = []
        for task_id, task in self.tasks.items():
            if task_id in completed_task_ids or task_id in active_task_ids:
                continue
            # Check if all dependencies are satisfied
            if all(dep in completed_task_ids for dep in task.dependencies):
                ready.append(task)
        return ready

    def topological_sort(self) -> List[TaskDefinition]:
        self.build_and_validate()
        in_degrees = {tid: len(self.tasks[tid].dependencies) for tid in self.tasks}
        queue = deque([tid for tid, deg in in_degrees.items() if deg == 0])
        ordered = []

        while queue:
            curr_id = queue.popleft()
            ordered.append(self.tasks[curr_id])
            for neighbor in self.adj_list[curr_id]:
                in_degrees[neighbor] -= 1
                if in_degrees[neighbor] == 0:
                    queue.append(neighbor)

        return ordered
