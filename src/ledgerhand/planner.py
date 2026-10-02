import hashlib
from pathlib import Path
from .models import GoalSpec
from .ledger import Task

def plan_tasks(inbox_files: list[Path], spec: GoalSpec) -> list[Task]:
    tasks = []
    # Sort files by filename to ensure deterministic ordering
    sorted_files = sorted(inbox_files, key=lambda p: p.name)
    
    for p in sorted_files:
        if p.is_file():
            content = p.read_bytes()
            h = hashlib.sha256(content).hexdigest()
            # Generate a deterministic task ID based on file hash and run specs
            task_id = "task_" + hashlib.sha256(f"{p.name}_{h}".encode()).hexdigest()[:12]
            
            t = Task(
                id=task_id,
                run_id="",  # Set by caller
                file_path=str(p),
                file_sha256=h,
                business_key="",
                status="pending",
                reason_code="",
                invoice_json="",
                checks_json="",
                decision_json="",
                updated_at=""
            )
            tasks.append(t)
            
    return tasks