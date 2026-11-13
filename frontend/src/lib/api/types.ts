/** Response types shared with the FastAPI contract. */

export type InvoiceStatus = {
  filename: string;
  matched: boolean;
  pushedToApi?: boolean;
};

export type MetricSummary = {
  unreadEmails?: number;
  invoicesProcessed?: number;
  invoicesFound?: number;
  rowsUpdated?: number;
  rowsPushedToApi?: number;
  apiPushSuccess?: number;
  apiPushFailed?: number;
  invoiceBreakdown?: Record<string, number>;
  invoiceList?: InvoiceStatus[];
};

export type InvoiceDbMatchFilter = "all" | "matched" | "unmatched" | "others";
export type InvoiceDataSource = "bills" | "xtract" | "btcbills";

export type InvoiceDbApiResponse = {
  columns: string[];
  rows: Record<string, unknown>[];
  count: number;
  total: number;
  match_filter: string;
  from_date: string;
  to_date: string;
  limit: number;
  offset: number;
  source?: string;
  table?: string;
};

export const INVOICE_DB_PAGE_SIZE = 500;

export type InvoiceStats = {
  matched: number;
  unmatched: number;
  others_total: number;
  food_invoices: number;
  hummingbird_mails: number;
  match_rate: number;
};

export type MailboxCounts = {
  emails: number;
  pdfs: number;
  emails_found: number;
  pdfs_found: number;
};

export type InvoiceDbSummary = {
  fromDate: string;
  toDate: string;
  emailsFound: number;
  emailsProcessed: number;
  invoicesFound: number;
  invoicesProcessed: number;
};

export type MatchRateTrendPoint = {
  date: string;
  matched: number;
  unmatched: number;
  match_rate: number;
};

export type MatchRateTrendResponse = {
  from_date: string;
  to_date: string;
  points: MatchRateTrendPoint[];
};

export type RunningProcessApiResponse = {
  running: boolean;
  task: {
    task_id: string;
    started_at: string;
    status: string;
  } | null;
};

export type TaskStatus = "running" | "success" | "error" | "cancelled";

export type TaskResponse = {
  task_id: string;
  status: TaskStatus;
  started_at: string;
  finished_at?: string;
  logs: string;
  error?: string | null;
  excel_link?: string | null;
};
