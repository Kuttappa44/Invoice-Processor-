import { motion } from "framer-motion";
import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Inbox,
  Layers,
  Mail,
  TrendingUp,
} from "lucide-react";
import type { InvoiceStats, MailboxCounts } from "../../lib/api/types";

type StatsKpisProps = {
  statsFromDate: string;
  statsToDate: string;
  statsLoading: boolean;
  stats: InvoiceStats | null;
  mailboxCounts: MailboxCounts | null;
  onFromDate: (value: string) => void;
  onToDate: (value: string) => void;
  onLoad: () => void;
};

export function StatsKpis({
  statsFromDate,
  statsToDate,
  statsLoading,
  stats,
  mailboxCounts,
  onFromDate,
  onToDate,
  onLoad,
}: StatsKpisProps) {
  return (
    <>
      <div className="stats-filter">
        <label className="stats-filter__field">
          <span>From</span>
          <input
            type="date"
            value={statsFromDate}
            onChange={(e) => onFromDate(e.target.value)}
          />
        </label>
        <label className="stats-filter__field">
          <span>To</span>
          <input
            type="date"
            value={statsToDate}
            onChange={(e) => onToDate(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="button button--gradient"
          onClick={onLoad}
          disabled={statsLoading}
        >
          <TrendingUp size={16} />
          {statsLoading ? "Loading…" : "Load Stats"}
        </button>
      </div>

      <div className="stats-kpi-rows">
      <div className="grid grid--4">
        <motion.div
          className="stat-tile stat-tile--slate"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.02 }}
        >
          <div className="stat-tile__icon">
            <Mail size={18} />
          </div>
          <span className="stat-tile__label">EMAILS</span>
          <span className="stat-tile__value">
            {mailboxCounts?.emails ?? "—"}
          </span>
          <span className="stat-tile__sub">mailbox volume</span>
        </motion.div>

        <motion.div
          className="stat-tile stat-tile--blue"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.04 }}
        >
          <div className="stat-tile__icon">
            <Mail size={18} />
          </div>
          <span className="stat-tile__label">EMAILS FOUND</span>
          <span className="stat-tile__value">
            {mailboxCounts?.emails_found ?? "—"}
          </span>
          <span className="stat-tile__sub">processed this run</span>
        </motion.div>

        <motion.div
          className="stat-tile stat-tile--violet"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.06 }}
        >
          <div className="stat-tile__icon">
            <FileText size={18} />
          </div>
          <span className="stat-tile__label">PDFS</span>
          <span className="stat-tile__value">
            {mailboxCounts?.pdfs ?? "—"}
          </span>
          <span className="stat-tile__sub">mailbox volume</span>
        </motion.div>

        <motion.div
          className="stat-tile stat-tile--pink"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.08 }}
        >
          <div className="stat-tile__icon">
            <FileText size={18} />
          </div>
          <span className="stat-tile__label">PDFS FOUND</span>
          <span className="stat-tile__value">
            {mailboxCounts?.pdfs_found ?? "—"}
          </span>
          <span className="stat-tile__sub">extracted this run</span>
        </motion.div>
      </div>

      <div className="grid grid--5">
        <motion.div
          className="stat-tile stat-tile--orange"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.05 }}
        >
          <div className="stat-tile__icon">
            <Layers size={18} />
          </div>
          <span className="stat-tile__label">TOTAL INVOICES</span>
          <span className="stat-tile__value">
            {stats ? stats.matched + stats.unmatched : "—"}
          </span>
          <span className="stat-tile__sub">matched + unmatched</span>
        </motion.div>

        <motion.div
          className="stat-tile stat-tile--green"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
        >
          <div className="stat-tile__icon">
            <CheckCircle2 size={18} />
          </div>
          <span className="stat-tile__label">MATCHED</span>
          <span className="stat-tile__value">{stats?.matched ?? "—"}</span>
        </motion.div>

        <motion.div
          className="stat-tile stat-tile--indigo"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.15 }}
        >
          <div className="stat-tile__icon">
            <AlertCircle size={18} />
          </div>
          <span className="stat-tile__label">UNMATCHED</span>
          <span className="stat-tile__value">{stats?.unmatched ?? "—"}</span>
        </motion.div>

        <motion.div
          className="stat-tile stat-tile--teal"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2 }}
        >
          <div className="stat-tile__icon">
            <Inbox size={18} />
          </div>
          <span className="stat-tile__label">OTHERS</span>
          <span className="stat-tile__value">
            {stats?.others_total ?? "—"}
          </span>
          <span className="stat-tile__sub">
            Food {stats?.food_invoices ?? 0} · Hummingbird{" "}
            {stats?.hummingbird_mails ?? 0}
          </span>
        </motion.div>

        <motion.div
          className="stat-tile stat-tile--red"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.25 }}
        >
          <div className="stat-tile__icon">
            <TrendingUp size={18} />
          </div>
          <span className="stat-tile__label">MATCH RATE</span>
          <span className="stat-tile__value">
            {stats ? `${stats.match_rate}%` : "—"}
          </span>
          <span className="stat-tile__sub">of total invoices</span>
        </motion.div>
      </div>
      </div>
    </>
  );
}
