import { AnimatePresence, motion } from "framer-motion";
import { formatDateTime } from "../../lib/dates";

type RunConflictDialogProps = {
  task: { task_id: string; started_at: string } | null;
  onKeep: () => void;
  onReplace: () => void;
};

export function RunConflictDialog({
  task,
  onKeep,
  onReplace,
}: RunConflictDialogProps) {
  return (
    <AnimatePresence>
      {task && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: "rgba(0, 0, 0, 0.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}
          onClick={onKeep}
        >
          <motion.div
            className="card"
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.95 }}
            onClick={(e) => e.stopPropagation()}
            style={{
              maxWidth: "480px",
              width: "90%",
              backgroundColor: "#fff",
              padding: "8px 0 0 0",
            }}
          >
            <header
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                padding: "0 20px 16px",
                borderBottom: "1px solid #e5e7eb",
              }}
            >
              <h2 className="card__title" style={{ margin: 0 }}>
                Run already in progress
              </h2>
              <button
                type="button"
                onClick={onKeep}
                style={{
                  background: "none",
                  border: "none",
                  fontSize: "24px",
                  cursor: "pointer",
                  color: "#666",
                  lineHeight: 1,
                }}
                title="Close"
              >
                ×
              </button>
            </header>
            <div style={{ padding: "20px" }}>
              <p style={{ fontSize: "15px", color: "#374151", margin: "0 0 12px" }}>
                A full flow is running on the server. Starting another now can corrupt database
                writes. Choose whether to keep the current run or stop it and start a new one
                (stop is cooperative and may take a short time between emails).
              </p>
              <p style={{ fontSize: "13px", color: "#6b7280", margin: "0 0 8px" }}>
                <strong>Task:</strong>{" "}
                <code style={{ fontSize: "12px" }}>{task.task_id}</code>
              </p>
              {task.started_at ? (
                <p style={{ fontSize: "13px", color: "#6b7280", margin: 0 }}>
                  <strong>Started:</strong> {formatDateTime(task.started_at)}
                </p>
              ) : null}
            </div>
            <div
              style={{
                display: "flex",
                gap: "12px",
                justifyContent: "flex-end",
                padding: "16px 20px",
                borderTop: "1px solid #e5e7eb",
                flexWrap: "wrap",
              }}
            >
              <button
                type="button"
                className="button button--outline"
                onClick={onKeep}
              >
                Keep current run
              </button>
              <button
                type="button"
                className="button button--primary"
                onClick={onReplace}
                title="Request cancellation of the current run, then start a new full flow"
              >
                Stop current & start new
              </button>
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
}
