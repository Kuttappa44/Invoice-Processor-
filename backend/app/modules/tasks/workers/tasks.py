"""Thread target for POST /api/process and POST /api/match-master."""

from __future__ import annotations

import contextlib
import re
import sys
import traceback
from datetime import datetime
from io import StringIO
from typing import Optional

from backend.app.exceptions import ProcessCancelled
from backend.app.modules.ingest.workers.tasks import run_process_invoices
from backend.app.modules.invoices import service as invoices_service
from backend.app.modules.tasks.registry import task_lock, tasks
from backend.app.modules.tasks.service import update_task_logs
from main import match_master_sheet_with_excel


def run_task_in_thread(task_id: str, task_type: str, from_date: Optional[str] = None, to_date: Optional[str] = None):
    """Run a task in a separate thread with real-time log capture"""
    global tasks

    cancel_ev = None
    # Update task status to running
    with task_lock:
        tasks[task_id]["status"] = "running"
        tasks[task_id]["started_at"] = datetime.now().isoformat()
        tasks[task_id]["logs"] = ""
        tasks[task_id]["error"] = None
        tasks[task_id]["excel_link"] = None  # Initialize Excel link
        cancel_ev = tasks[task_id].get("cancel_event")

    # Create a custom stream that updates task logs in real-time AND prints to terminal
    class LogStream(StringIO):
        def __init__(self, task_id: str, is_error: bool = False):
            super().__init__()
            self.task_id = task_id
            self.buffer = ""
            self.is_error = is_error
            # Keep reference to original stdout/stderr for terminal output
            self.original_stream = sys.stderr if is_error else sys.stdout
        
        def write(self, s: str) -> int:
            # Print to terminal immediately (for backend visibility)
            if s:
                try:
                    self.original_stream.write(s)
                    self.original_stream.flush()
                except:
                    pass  # Ignore errors if terminal is unavailable
            
            # Accumulate buffer for task logs
            self.buffer += s
            
            # Check for Excel link marker in real-time and update task immediately
            # Check both in current buffer and combined buffer to handle split writes
            combined_text = self.buffer
            if 'EXCEL_LINK_START:' in combined_text and ':EXCEL_LINK_END' in combined_text:
                try:
                    link_match = re.search(r'EXCEL_LINK_START:(.+?):EXCEL_LINK_END', combined_text, re.DOTALL)
                    if link_match:
                        excel_link = link_match.group(1).strip()
                        # Update task Excel link immediately
                        with task_lock:
                            if task_id in tasks:
                                tasks[task_id]["excel_link"] = excel_link
                        # Remove the marker from buffer so it doesn't appear in logs
                        self.buffer = re.sub(r'EXCEL_LINK_START:.+?:EXCEL_LINK_END\s*', '', self.buffer, flags=re.DOTALL)
                        # Flush immediately to ensure logs are updated
                        self.flush()
                except Exception as e:
                    pass  # Ignore errors in link extraction
            
            # If buffer ends with newline or gets large, flush it
            if '\n' in self.buffer or len(self.buffer) > 512:
                self.flush()
            return len(s)
        
        def flush(self):
            # Check for Excel link marker one more time before flushing (in case marker was split)
            if 'EXCEL_LINK_START:' in self.buffer and ':EXCEL_LINK_END' in self.buffer:
                try:
                    link_match = re.search(r'EXCEL_LINK_START:(.+?):EXCEL_LINK_END', self.buffer, re.DOTALL)
                    if link_match:
                        excel_link = link_match.group(1).strip()
                        # Update task Excel link immediately
                        with task_lock:
                            if task_id in tasks:
                                tasks[task_id]["excel_link"] = excel_link
                        # Remove the marker from buffer so it doesn't appear in logs
                        self.buffer = re.sub(r'EXCEL_LINK_START:.+?:EXCEL_LINK_END\s*', '', self.buffer, flags=re.DOTALL)
                except Exception as e:
                    pass  # Ignore errors in link extraction
            
            if self.buffer:
                update_task_logs(self.task_id, self.buffer)
                self.buffer = ""
            super().flush()
            # Also flush original stream
            try:
                self.original_stream.flush()
            except:
                pass
        
        def close(self):
            # Flush remaining buffer
            if self.buffer:
                update_task_logs(self.task_id, self.buffer)
            super().close()
    
    try:
        # Create log streams for stdout and stderr (both print to terminal and capture for frontend)
        log_stream = LogStream(task_id, is_error=False)
        error_stream = LogStream(task_id, is_error=True)
        
        # Redirect stdout and stderr
        with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(error_stream):
            # Execute the appropriate task function
            if task_type == "process":
                result = run_process_invoices(
                    from_date=from_date, to_date=to_date, cancel_event=cancel_ev
                )
            elif task_type == "match-master":
                result = match_master_sheet_with_excel()
            else:
                raise ValueError(f"Unknown task type: {task_type}")
        
        # Flush any remaining logs
        log_stream.flush()
        error_stream.flush()
        log_stream.close()
        error_stream.close()
        
        # Update task status to success and store Excel link if available
        with task_lock:
            tasks[task_id]["status"] = "success"
            tasks[task_id]["finished_at"] = datetime.now().isoformat()
            # Store Excel link if process_invoices returned one
            if task_type == "process" and result:
                tasks[task_id]["excel_link"] = result
        
        # Force refresh of Excel path after processing completes
        if task_type == "process":
            path = invoices_service.refresh_excel_path_from_main()
            print(f"🔄 Refreshed Excel path after processing: {path}")

    except ProcessCancelled:
        update_task_logs(task_id, "\n⏹️ Run cancelled by user.\n")
        try:
            log_stream.flush()
            error_stream.flush()
            log_stream.close()
            error_stream.close()
        except Exception:
            pass
        with task_lock:
            if task_id in tasks:
                tasks[task_id]["status"] = "cancelled"
                tasks[task_id]["finished_at"] = datetime.now().isoformat()
                tasks[task_id]["error"] = None
        return

    except Exception as e:
        # Capture error in logs
        error_msg = str(e)
        error_traceback = traceback.format_exc()
        
        # Update task logs with error
        error_log = f"\n\n❌ ERROR: {error_msg}\n\nTraceback:\n{error_traceback}\n"
        update_task_logs(task_id, error_log)
        
        # Update task status to error
        with task_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = error_msg
            tasks[task_id]["finished_at"] = datetime.now().isoformat()
