import { useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { Activity, AlertCircle, CheckCircle2, Clock } from "lucide-react";
import type { TaskRecord } from "../../hooks/useTaskRunner";
import { formatDateTime, formatDuration } from "../../lib/dates";

const LogViewer = ({ logs, error, isRunning }: { logs: string; error?: string | null; isRunning: boolean }) => {
  const logRef = useRef<HTMLDivElement>(null);
  const prevLogsLengthRef = useRef<number>(0);

  useEffect(() => {
    // Auto-scroll to bottom when new logs arrive
    if (logRef.current && logs.length > prevLogsLengthRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
    prevLogsLengthRef.current = logs.length;
  }, [logs]);

  return (
    <div
      ref={logRef}
      style={{
        backgroundColor: "#1f2544",
        color: "#e5e7f3",
        padding: "16px",
        borderRadius: "8px",
        fontFamily: "monospace",
        fontSize: "13px",
        lineHeight: "1.6",
        maxHeight: "400px",
        overflowY: "auto",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        position: "relative",
      }}
    >
      {logs}
      {isRunning && logs && (
        <span
          style={{
            display: "inline-block",
            width: "8px",
            height: "13px",
            backgroundColor: "#C41E3A",
            marginLeft: "4px",
            animation: "blink 1s infinite",
          }}
        />
      )}
      {error && (
        <div style={{ marginTop: "16px", padding: "12px", backgroundColor: "#7f1d1d", borderRadius: "4px", color: "#fecaca" }}>
          <strong>Error:</strong> {error}
        </div>
      )}
      <style>{`
        @keyframes blink {
          0%, 50% { opacity: 1; }
          51%, 100% { opacity: 0; }
        }
      `}</style>
    </div>
  );
};

export function TaskLogs({ task }: { task: TaskRecord }) {
  return (
    <motion.section
      className="card"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.2 }}
    >
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
        <div>
          <h2 className="card__title">Task Logs</h2>
          <p style={{ fontSize: "14px", color: "#666", marginTop: "4px" }}>
            {task.task_id.startsWith("process") ? "Full Flow" : "Master Sync"} - {task.status}
          </p>
        </div>
        {task.status === "running" && (
          <div style={{ display: "flex", alignItems: "center", gap: "8px", color: "#C41E3A" }}>
            <Activity size={16} className="animate-pulse" />
            <span style={{ fontSize: "14px", fontWeight: 500 }}>Running...</span>
          </div>
        )}
        {task.status === "success" && (
          <div style={{ display: "flex", alignItems: "center", gap: "8px", color: "#10b981" }}>
            <CheckCircle2 size={16} />
            <span style={{ fontSize: "14px", fontWeight: 500 }}>Completed</span>
          </div>
        )}
        {task.status === "error" && (
          <div style={{ display: "flex", alignItems: "center", gap: "8px", color: "#ef4444" }}>
            <AlertCircle size={16} />
            <span style={{ fontSize: "14px", fontWeight: 500 }}>Failed</span>
          </div>
        )}
        {task.status === "cancelled" && (
          <div style={{ display: "flex", alignItems: "center", gap: "8px", color: "#6b7280" }}>
            <Clock size={16} />
            <span style={{ fontSize: "14px", fontWeight: 500 }}>Cancelled</span>
          </div>
        )}
      </header>
      <LogViewer
        logs={task.logs || "No logs available yet..."}
        error={task.error}
        isRunning={task.status === "running"}
      />
      {task.finished_at && (
        <div style={{ marginTop: "12px", fontSize: "12px", color: "#666" }}>
          Completed: {formatDateTime(task.finished_at)} ({formatDuration(task.started_at, task.finished_at)})
        </div>
      )}
    </motion.section>
  );
}
