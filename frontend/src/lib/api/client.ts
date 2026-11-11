/** The only HTTP client. Screens and hooks call these helpers, not handwritten URLs. */

import axios from "axios";
import type {
  InvoiceDataSource,
  InvoiceDbMatchFilter,
  TaskResponse,
} from "./types";

export function getApiBaseUrl() {
  // If explicitly set, use it (remove trailing slash if present)
  if (import.meta.env.VITE_API_BASE_URL) {
    const url = import.meta.env.VITE_API_BASE_URL;
    return url.endsWith("/") ? url.slice(0, -1) : url;
  }
  // In dev mode, use relative paths (Vite proxy will handle it)
  if (import.meta.env.DEV) {
    return "";
  }
  // In production: use relative paths (works when frontend and backend are served from same origin/proxy)
  // For Jarvis Labs: both frontend and backend are served through /proxy/8000, so relative paths work
  return "";
}

export const API_BASE_URL = getApiBaseUrl();

export const LOGO_SRC = "/api/logo";

const jsonHeaders = { headers: { "Content-Type": "application/json" } };

function withQuery(path: string, params: Record<string, string>) {
  return `${API_BASE_URL}${path}?${new URLSearchParams(params)}`;
}

export function getMysqlStats(fromDate: string, toDate: string, source: InvoiceDataSource) {
  return fetch(
    withQuery("/api/mysql/stats", {
      from_date: fromDate,
      to_date: toDate,
      source,
    }),
  );
}

export function getMailboxCounts(fromDate: string, toDate: string, source: InvoiceDataSource) {
  return fetch(
    withQuery("/api/mysql/mailbox-counts", {
      from_date: fromDate,
      to_date: toDate,
      source,
    }),
  );
}

export function getMatchRateTrend(fromDate: string, toDate: string, source: InvoiceDataSource) {
  return fetch(
    withQuery("/api/mysql/match-rate-trend", {
      from_date: fromDate,
      to_date: toDate,
      source,
    }),
  );
}

export function getRunningProcess() {
  return fetch(`${API_BASE_URL}/api/tasks/running-process`);
}

export function getInvoiceRows(args: {
  fromDate: string;
  toDate: string;
  match: InvoiceDbMatchFilter;
  source: InvoiceDataSource;
  limit: number;
  offset: number;
}) {
  return fetch(
    withQuery("/api/mysql/rows", {
      from_date: args.fromDate,
      to_date: args.toDate,
      match: args.match,
      source: args.source,
      limit: String(args.limit),
      offset: String(args.offset),
    }),
  );
}

export function getInvoiceExportByDate(args: {
  fromDate: string;
  toDate: string;
  match: InvoiceDbMatchFilter;
  source: InvoiceDataSource;
}) {
  return fetch(
    withQuery("/api/mysql/export-by-date", {
      from_date: args.fromDate,
      to_date: args.toDate,
      match: args.match,
      source: args.source,
    }),
  );
}

export function getTask(taskId: string) {
  return axios.get<TaskResponse>(`${API_BASE_URL}/api/tasks/${taskId}`);
}

export function postProcessTask(body: {
  from_date: string | null;
  to_date: string | null;
  force_replace: boolean;
}) {
  return axios.post<{ task_id: string }>(
    `${API_BASE_URL}/api/process`,
    body,
    jsonHeaders,
  );
}

export function postMatchMasterTask() {
  return axios.post<{ task_id: string }>(
    `${API_BASE_URL}/api/match-master`,
    {},
    jsonHeaders,
  );
}
