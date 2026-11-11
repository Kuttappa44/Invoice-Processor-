import { Play } from "lucide-react";
import { LOGO_SRC } from "../../lib/api/client";

type DashboardHeaderProps = {
  fromDate: string;
  toDate: string;
  isRunning: boolean;
  onFromDate: (value: string) => void;
  onToDate: (value: string) => void;
  onStart: () => void;
};

export function DashboardHeader({
  fromDate,
  toDate,
  isRunning,
  onFromDate,
  onToDate,
  onStart,
}: DashboardHeaderProps) {
  return (
    <>
      <header className="main__header">
        <div className="main__title-group">
          <img
            src={LOGO_SRC}
            alt="Logo"
            style={{
              height: "72px",
              width: "auto",
              objectFit: "contain",
              maxWidth: "320px",
              display: "block"
            }}
            onError={(e) => {
              const img = e.target as HTMLImageElement;
              console.warn("Logo image not found at:", img.src);
              img.style.display = "none";
            }}
          />
          <h1 className="main__title">Invoice Automation Hub</h1>
        </div>
        <div className="main__actions">
          <div style={{ display: "flex", flexDirection: "column", gap: "16px", alignItems: "flex-end" }}>
            <div style={{ display: "flex", gap: "16px", alignItems: "center" }}>
              <label style={{ fontSize: "14px", color: "#666", display: "flex", alignItems: "center", gap: "10px" }}>
                <span>From:</span>
                <input
                  type="date"
                  value={fromDate}
                  onChange={(e) => onFromDate(e.target.value)}
                  disabled={isRunning}
                  style={{
                    padding: "6px 10px",
                    border: "1px solid #ddd",
                    borderRadius: "4px",
                    fontSize: "14px",
                    fontFamily: "inherit"
                  }}
                />
              </label>
              <label style={{ fontSize: "14px", color: "#666", display: "flex", alignItems: "center", gap: "10px" }}>
                <span>To:</span>
                <input
                  type="date"
                  value={toDate}
                  onChange={(e) => onToDate(e.target.value)}
                  disabled={isRunning}
                  style={{
                    padding: "6px 10px",
                    border: "1px solid #ddd",
                    borderRadius: "4px",
                    fontSize: "14px",
                    fontFamily: "inherit"
                  }}
                />
              </label>
            </div>
            <div style={{ display: "flex", gap: "12px" }}>
              <button
                className="button button--primary"
                disabled={true}
                onClick={onStart}
                title={isRunning ? "Processing in progress..." : "Run full invoice processing flow (Excel auto-generated from API)"}
              >
                <Play size={16} />
                Run Full Flow
              </button>
            </div>
          </div>
        </div>
      </header>
    </>
  );
}
