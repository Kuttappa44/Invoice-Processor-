import { motion } from "framer-motion";
import { Database, Download, Search } from "lucide-react";
import {
  INVOICE_DB_PAGE_SIZE,
  type InvoiceDbMatchFilter,
  type InvoiceDbSummary,
} from "../../lib/api/types";

type InvoiceDatabaseProps = {
  fromDate: string;
  toDate: string;
  match: InvoiceDbMatchFilter;
  columns: string[];
  rows: Record<string, unknown>[];
  meta: { count: number; total: number; limit: number; offset: number } | null;
  page: number;
  loading: boolean;
  error: string | null;
  summary: InvoiceDbSummary | null;
  onFromDate: (value: string) => void;
  onToDate: (value: string) => void;
  onMatch: (value: InvoiceDbMatchFilter) => void;
  onLoad: (page: number) => void;
  onExport: () => void;
};

const formatInvoiceDbCell = (v: unknown): string => {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
};

export function InvoiceDatabase({
  fromDate,
  toDate,
  match,
  columns,
  rows,
  meta,
  page,
  loading,
  error,
  summary,
  onFromDate,
  onToDate,
  onMatch,
  onLoad,
  onExport,
}: InvoiceDatabaseProps) {
  return (
    <motion.section
      className="card invoice-db-card"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.18 }}
    >
      <div className="invoice-db__header">
        <Database size={22} className="invoice-db__icon" aria-hidden />
        <div>
          <h2 className="card__title invoice-db__title">Invoice Database</h2>
        </div>
      </div>

      <div className="invoice-db__toolbar">
        <label className="invoice-db__field">
          <span>From</span>
          <input
            type="date"
            value={fromDate}
            onChange={(e) => onFromDate(e.target.value)}
          />
        </label>
        <label className="invoice-db__field">
          <span>To</span>
          <input
            type="date"
            value={toDate}
            onChange={(e) => onToDate(e.target.value)}
          />
        </label>
        <label className="invoice-db__field">
          <span>Match</span>
          <select
            value={match}
            onChange={(e) =>
              onMatch(e.target.value as InvoiceDbMatchFilter)
            }
          >
            <option value="all">All</option>
            <option value="matched">Matched</option>
            <option value="unmatched">Unmatched</option>
            <option value="others">Others</option>
          </select>
        </label>
        <button
          type="button"
          className="button button--primary"
          onClick={() => onLoad(1)}
          disabled={
            loading ||
            !fromDate ||
            !toDate
          }
        >
          <Search size={16} />
          {loading ? "Loading…" : "Load data"}
        </button>
        <button
          type="button"
          className="button button--outline"
          onClick={onExport}
          disabled={!fromDate || !toDate}
          title="Download Excel for this date range and match filter (all matching rows, not just the preview)"
        >
          <Download size={16} />
          Export to Excel
        </button>
      </div>

      {error && (
        <p className="invoice-db__error" role="alert">
          {error}
        </p>
      )}

      {summary && (
        <div className="invoice-db__summary" aria-label="Mailbox totals for this date range">
          <div className="invoice-db__summary-item">
            <span className="invoice-db__summary-label">Dates</span>
            <span className="invoice-db__summary-value invoice-db__summary-value--dates">
              {summary.fromDate} – {summary.toDate}
            </span>
          </div>
          <div className="invoice-db__summary-item">
            <span className="invoice-db__summary-label">Emails found</span>
            <span className="invoice-db__summary-value">{summary.emailsFound}</span>
          </div>
          <div className="invoice-db__summary-item">
            <span className="invoice-db__summary-label">Emails processed</span>
            <span className="invoice-db__summary-value">{summary.emailsProcessed}</span>
          </div>
          <div className="invoice-db__summary-item">
            <span className="invoice-db__summary-label">Invoices found</span>
            <span className="invoice-db__summary-value">{summary.invoicesFound}</span>
          </div>
          <div className="invoice-db__summary-item">
            <span className="invoice-db__summary-label">Invoices processed</span>
            <span className="invoice-db__summary-value">{summary.invoicesProcessed}</span>
          </div>
        </div>
      )}

      {meta && (
        <p className="invoice-db__meta">
          {meta.total > 0
            ? `Showing ${meta.offset + 1}–${
                meta.offset + meta.count
              } of ${meta.total} row(s)`
            : "Showing 0 row(s)"}
        </p>
      )}

      <div className="invoice-db__table-wrap">
        {columns.length > 0 ? (
          <table className="invoice-db__table">
            <thead>
              <tr>
                {columns.map((col) => (
                  <th key={col} title={col}>
                    {col.replace(/_/g, " ")}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={ri}>
                  {columns.map((col) => (
                    <td key={col}>
                      {formatInvoiceDbCell(row[col])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="invoice-db__empty">
            {loading
              ? "Loading…"
              : "Pick a date range and click Load data to preview rows."}
          </p>
        )}
      </div>

      {meta && meta.total > INVOICE_DB_PAGE_SIZE && (
        <div className="invoice-db__pagination">
          <button
            type="button"
            className="button button--outline"
            onClick={() => onLoad(page - 1)}
            disabled={loading || page <= 1}
          >
            Prev
          </button>
          <span className="invoice-db__page-label">
            Page {page} of{" "}
            {Math.max(
              1,
              Math.ceil(meta.total / INVOICE_DB_PAGE_SIZE),
            )}
          </span>
          <button
            type="button"
            className="button button--outline"
            onClick={() => onLoad(page + 1)}
            disabled={
              loading ||
              page >=
                Math.ceil(meta.total / INVOICE_DB_PAGE_SIZE)
            }
          >
            Next
          </button>
        </div>
      )}
    </motion.section>
  );
}
