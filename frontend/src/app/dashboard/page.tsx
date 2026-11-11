import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { useTaskRunner } from "../../hooks/useTaskRunner";
import {
  API_BASE_URL,
  getInvoiceExportByDate,
  getInvoiceRows,
  getMailboxCounts,
  getMatchRateTrend,
  getMysqlStats,
  getRunningProcess,
} from "../../lib/api/client";
import {
  INVOICE_DB_PAGE_SIZE,
  type InvoiceDataSource,
  type InvoiceDbApiResponse,
  type InvoiceDbMatchFilter,
  type InvoiceStats,
  type MailboxCounts,
  type MatchRateTrendPoint,
  type MatchRateTrendResponse,
  type RunningProcessApiResponse,
} from "../../lib/api/types";
import { getDefaultCurrentMonthRange } from "../../lib/dates";
import { DashboardHeader } from "./dashboard-header";
import { InvoiceDatabase } from "./invoice-database";
import { MatchRateChart } from "./match-rate-chart";
import { RunConflictDialog } from "./run-conflict-dialog";
import { SourceFilter } from "./source-filter";
import { StatsKpis } from "./stats-kpis";
import { TaskLogs } from "./task-logs";

export function DashboardPage() {
  console.log("📱 App component rendering...");

  const { tasks, isRunning, startTask } = useTaskRunner();
  const statusRef = useRef<Record<string, string>>({});
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [appError, setAppError] = useState<string | null>(null);
  const [fromDate, setFromDate] = useState<string>("");
  const [toDate, setToDate] = useState<string>("");
  const [dataSource, setDataSource] = useState<InvoiceDataSource>("bills");
  const [statsFromDate, setStatsFromDate] = useState<string>("");
  const [statsToDate, setStatsToDate] = useState<string>("");
  const [statsLoading, setStatsLoading] = useState(false);
  const [invoiceDbFromDate, setInvoiceDbFromDate] = useState<string>("");
  const [invoiceDbToDate, setInvoiceDbToDate] = useState<string>("");
  const [invoiceDbMatch, setInvoiceDbMatch] =
    useState<InvoiceDbMatchFilter>("all");
  const [invoiceDbColumns, setInvoiceDbColumns] = useState<string[]>([]);
  const [invoiceDbRows, setInvoiceDbRows] = useState<Record<string, unknown>[]>(
    [],
  );
  const [invoiceDbMeta, setInvoiceDbMeta] = useState<{
    count: number;
    total: number;
    limit: number;
    offset: number;
  } | null>(null);
  const [invoiceDbPage, setInvoiceDbPage] = useState(1);
  const [invoiceDbLoading, setInvoiceDbLoading] = useState(false);
  const [invoiceDbError, setInvoiceDbError] = useState<string | null>(null);
  const [stats, setStats] = useState<InvoiceStats | null>(null);
  const [mailboxCounts, setMailboxCounts] = useState<MailboxCounts | null>(
    null,
  );
  const [graphFromDate, setGraphFromDate] = useState(
    () => getDefaultCurrentMonthRange().from,
  );
  const [graphToDate, setGraphToDate] = useState(
    () => getDefaultCurrentMonthRange().to,
  );
  const [graphPoints, setGraphPoints] = useState<MatchRateTrendPoint[]>([]);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [runConflictTask, setRunConflictTask] = useState<{
    task_id: string;
    started_at: string;
  } | null>(null);

  useEffect(() => {
    if (!selectedTaskId && tasks.length > 0) {
      setSelectedTaskId(tasks[0].task_id);
    }
  }, [selectedTaskId, tasks]);


  useEffect(() => {
    tasks.forEach((task) => {
      const previous = statusRef.current[task.task_id];
      if (!previous && task.status === "running") {
        statusRef.current[task.task_id] = task.status;
        return;
      }
      if (previous !== task.status && task.status !== "running") {
        statusRef.current[task.task_id] = task.status;
        if (task.status === "success") {
          toast.success(
            `${
              task.task_id.startsWith("process")
                ? "Full flow"
                : "Master sync"
            } completed`,
          );
        } else if (task.status === "error") {
          toast.error(
            task.error ??
              `${
                task.task_id.startsWith("process")
                  ? "Full flow"
                  : "Master sync"
              } failed`,
          );
        } else if (task.status === "cancelled") {
          toast(
            `${
              task.task_id.startsWith("process")
                ? "Full flow"
                : "Master sync"
            } was cancelled`,
            { icon: "⏹️" },
          );
        }
      }
      statusRef.current[task.task_id] = task.status;
    });
  }, [tasks]);


  const selectedTask = useMemo(
    () => tasks.find((task) => task.task_id === selectedTaskId),
    [selectedTaskId, tasks],
  );

  const loadStats = useCallback(async () => {
    if (!statsFromDate || !statsToDate) {
      toast.error("Select both From and To dates");
      return;
    }
    if (statsFromDate > statsToDate) {
      toast.error("From date must be before or equal to To date");
      return;
    }
    setStatsLoading(true);
    try {
      const [statsRes, mailboxRes] = await Promise.all([
        getMysqlStats(statsFromDate, statsToDate, dataSource),
        getMailboxCounts(statsFromDate, statsToDate, dataSource),
      ]);
      if (!statsRes.ok) {
        throw new Error(await statsRes.text());
      }
      setStats((await statsRes.json()) as InvoiceStats);
      if (mailboxRes.ok) {
        const mailbox = (await mailboxRes.json()) as MailboxCounts;
        setMailboxCounts({
          emails: mailbox.emails ?? 0,
          pdfs: mailbox.pdfs ?? 0,
          emails_found: mailbox.emails_found ?? 0,
          pdfs_found: mailbox.pdfs_found ?? 0,
        });
      } else {
        setMailboxCounts(null);
        toast.error("Failed to load mailbox email/PDF counts");
      }
    } catch {
      setStats(null);
      setMailboxCounts(null);
      toast.error("Failed to load stats");
    } finally {
      setStatsLoading(false);
    }
  }, [statsFromDate, statsToDate, dataSource]);

  const loadMatchRateGraph = useCallback(async () => {
    if (!graphFromDate || !graphToDate) {
      toast.error("Select both From and To dates for the graph");
      return;
    }
    if (graphFromDate > graphToDate) {
      toast.error("From date must be before or equal to To date");
      return;
    }
    setGraphLoading(true);
    setGraphError(null);
    try {
      const res = await getMatchRateTrend(graphFromDate, graphToDate, dataSource);
      if (!res.ok) {
        throw new Error(await res.text());
      }
      const data = (await res.json()) as MatchRateTrendResponse;
      setGraphPoints(data.points ?? []);
    } catch {
      setGraphPoints([]);
      setGraphError("Failed to load match rate trend");
      toast.error("Failed to load match rate graph");
    } finally {
      setGraphLoading(false);
    }
  }, [graphFromDate, graphToDate, dataSource]);

  const graphInitialLoad = useRef(false);
  useEffect(() => {
    if (graphInitialLoad.current) return;
    graphInitialLoad.current = true;
    loadMatchRateGraph();
  }, [loadMatchRateGraph]);

  // Clear loaded data when switching Bills ↔ Xtract; reload chart if dates set.
  useEffect(() => {
    setStats(null);
    setMailboxCounts(null);
    setInvoiceDbColumns([]);
    setInvoiceDbRows([]);
    setInvoiceDbMeta(null);
    setInvoiceDbError(null);
    setInvoiceDbPage(1);
    if (graphInitialLoad.current && graphFromDate && graphToDate) {
      loadMatchRateGraph();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to source changes
  }, [dataSource]);

  const handleStart = async () => {
    // Validate date range
    if (fromDate && toDate && new Date(fromDate) > new Date(toDate)) {
      toast.error("From date must be before or equal to To date");
      return;
    }

    try {
      const runningRes = await getRunningProcess();
      if (!runningRes.ok) {
        toast.error("Could not check whether a run is already active");
        return;
      }
      const runningBody = (await runningRes.json()) as RunningProcessApiResponse;
      if (runningBody.running && runningBody.task) {
        setRunConflictTask({
          task_id: runningBody.task.task_id,
          started_at: runningBody.task.started_at,
        });
        return;
      }
    } catch {
      toast.error("Could not reach the server to check run status");
      return;
    }

    const toastId = toast.loading("Invoice processing kicked off…");
    try {
      const taskId = await startTask("process", { fromDate, toDate });
      toast.success("Task started, monitoring progress", { id: toastId });
      setSelectedTaskId(taskId);
    } catch (error: any) {
      const detail = error?.response?.data?.detail;
      const runningId =
        typeof detail === "object" && detail?.running_task_id
          ? String(detail.running_task_id)
          : null;
      if (error?.response?.status === 409 && runningId) {
        toast.dismiss(toastId);
        setRunConflictTask({
          task_id: runningId,
          started_at: "",
        });
        return;
      }
      const msg =
        typeof detail === "string"
          ? detail
          : detail?.message ?? error?.message ?? "Failed to trigger task";
      toast.error(msg, { id: toastId });
    }
  };

  const handleStartAfterReplace = async () => {
    setRunConflictTask(null);
    if (fromDate && toDate && new Date(fromDate) > new Date(toDate)) {
      toast.error("From date must be before or equal to To date");
      return;
    }
    const toastId = toast.loading(
      "Stopping the current run (if needed) and starting a new one…",
    );
    try {
      const taskId = await startTask("process", {
        fromDate,
        toDate,
        forceReplace: true,
      });
      toast.success("New run started", { id: toastId });
      setSelectedTaskId(taskId);
    } catch (error: any) {
      const detail = error?.response?.data?.detail;
      const msg =
        typeof detail === "string"
          ? detail
          : detail?.message ?? error?.message ?? "Failed to start task";
      toast.error(msg, { id: toastId });
    }
  };

  const loadInvoiceDatabase = useCallback(
    async (page: number = 1) => {
      if (!invoiceDbFromDate || !invoiceDbToDate) {
        toast.error("Select both From and To dates");
        return;
      }
      if (invoiceDbFromDate > invoiceDbToDate) {
        toast.error("From date must be before or equal to To date");
        return;
      }
      const targetPage = Math.max(1, page);
      setInvoiceDbLoading(true);
      setInvoiceDbError(null);
      try {
        const res = await getInvoiceRows({
          fromDate: invoiceDbFromDate,
          toDate: invoiceDbToDate,
          match: invoiceDbMatch,
          source: dataSource,
          limit: INVOICE_DB_PAGE_SIZE,
          offset: (targetPage - 1) * INVOICE_DB_PAGE_SIZE,
        });
        if (!res.ok) {
          const t = await res.text();
          throw new Error(t || "Failed to load invoice data");
        }
        const data = (await res.json()) as InvoiceDbApiResponse;
        setInvoiceDbColumns(data.columns ?? []);
        setInvoiceDbRows(data.rows ?? []);
        setInvoiceDbMeta({
          count: data.count ?? 0,
          total: data.total ?? 0,
          limit: data.limit ?? INVOICE_DB_PAGE_SIZE,
          offset: data.offset ?? 0,
        });
        setInvoiceDbPage(targetPage);
        if (!data.rows?.length) {
          toast.success("No rows in this range / filter");
        } else {
          toast.success(`Loaded ${data.rows.length} row(s)`);
        }
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "Failed to load data";
        setInvoiceDbError(msg);
        setInvoiceDbColumns([]);
        setInvoiceDbRows([]);
        setInvoiceDbMeta(null);
        toast.error(msg);
      } finally {
        setInvoiceDbLoading(false);
      }
    },
    [invoiceDbFromDate, invoiceDbToDate, invoiceDbMatch, dataSource],
  );

  const exportInvoiceDatabaseExcel = useCallback(async () => {
    if (!invoiceDbFromDate || !invoiceDbToDate) {
      toast.error("Select both From and To dates");
      return;
    }
    if (invoiceDbFromDate > invoiceDbToDate) {
      toast.error("From date must be before or equal to To date");
      return;
    }
    const toastId = toast.loading("Exporting to Excel…");
    try {
      const response = await getInvoiceExportByDate({
        fromDate: invoiceDbFromDate,
        toDate: invoiceDbToDate,
        match: invoiceDbMatch,
        source: dataSource,
      });
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || "Export failed");
      }
      const contentDisposition = response.headers.get("Content-Disposition");
      let filename = `invoice_export_${dataSource}_${invoiceDbFromDate}_to_${invoiceDbToDate}.xlsx`;
      if (contentDisposition) {
        const filenameMatch = contentDisposition.match(/filename="?(.+)"?/);
        if (filenameMatch) {
          filename = filenameMatch[1];
        }
      }
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
      toast.success("Excel downloaded", { id: toastId });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Export failed";
      toast.error(msg, { id: toastId });
    }
  }, [invoiceDbFromDate, invoiceDbToDate, invoiceDbMatch, dataSource]);

  // Show error banner if there's a connection issue
  if (appError && appError.includes("fetch")) {
    console.warn("⚠️ Connection error detected:", appError);
  }

  return (
    <>
      {appError && (
        <div style={{
          background: "#fee",
          border: "2px solid #f00",
          padding: "12px 20px",
          margin: "0",
          textAlign: "center",
          fontFamily: "sans-serif",
          fontSize: "14px"
        }}>
          <strong>⚠️ Connection Error:</strong> {appError}
          <br />
          <small style={{ color: "#666" }}>
            Make sure the backend is running at <code>{API_BASE_URL}</code>
          </small>
        </div>
      )}
      <div className="app-shell">
        <main className="main main--full">
          <DashboardHeader
            fromDate={fromDate}
            toDate={toDate}
            isRunning={isRunning}
            onFromDate={setFromDate}
            onToDate={setToDate}
            onStart={handleStart}
          />

          <div className="main__content">
          <SourceFilter dataSource={dataSource} onChange={setDataSource} />

          <StatsKpis
            statsFromDate={statsFromDate}
            statsToDate={statsToDate}
            statsLoading={statsLoading}
            stats={stats}
            mailboxCounts={mailboxCounts}
            onFromDate={setStatsFromDate}
            onToDate={setStatsToDate}
            onLoad={loadStats}
          />

          <MatchRateChart
            graphFromDate={graphFromDate}
            graphToDate={graphToDate}
            graphPoints={graphPoints}
            graphLoading={graphLoading}
            graphError={graphError}
            onFromDate={setGraphFromDate}
            onToDate={setGraphToDate}
            onLoad={loadMatchRateGraph}
          />

          <div className="grid grid--1">
            <InvoiceDatabase
              fromDate={invoiceDbFromDate}
              toDate={invoiceDbToDate}
              match={invoiceDbMatch}
              columns={invoiceDbColumns}
              rows={invoiceDbRows}
              meta={invoiceDbMeta}
              page={invoiceDbPage}
              loading={invoiceDbLoading}
              error={invoiceDbError}
              onFromDate={setInvoiceDbFromDate}
              onToDate={setInvoiceDbToDate}
              onMatch={setInvoiceDbMatch}
              onLoad={loadInvoiceDatabase}
              onExport={exportInvoiceDatabaseExcel}
            />

            {selectedTask && <TaskLogs task={selectedTask} />}

          </div>
          </div>
        </main>
      </div>

      <RunConflictDialog
        task={runConflictTask}
        onKeep={() => setRunConflictTask(null)}
        onReplace={handleStartAfterReplace}
      />

      <Toaster position="top-right" toastOptions={{ duration: 4000 }} />
    </>
  );
}
