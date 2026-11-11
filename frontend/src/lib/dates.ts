/** Date formatting used by the dashboard. */

export const getDefaultCurrentMonthRange = () => {
  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth();
  const monthStr = String(month + 1).padStart(2, "0");
  const lastDay = new Date(year, month + 1, 0).getDate();
  return {
    from: `${year}-${monthStr}-01`,
    to: `${year}-${monthStr}-${String(lastDay).padStart(2, "0")}`,
  };
};

export const formatChartDate = (isoDate: string) => {
  try {
    const d = new Date(`${isoDate}T00:00:00`);
    return new Intl.DateTimeFormat("en-US", {
      month: "short",
      day: "numeric",
    }).format(d);
  } catch {
    return isoDate;
  }
};

export const formatDateTime = (value?: string) => {
  if (!value) return "—";
  try {
    const date = new Date(value);
    return new Intl.DateTimeFormat("en-GB", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  } catch {
    return value;
  }
};

export const formatDuration = (start?: string, end?: string) => {
  if (!start) return "—";
  const startDate = new Date(start);
  const endDate = end ? new Date(end) : new Date();
  const diffMs = endDate.getTime() - startDate.getTime();
  const diffSec = Math.max(0, Math.round(diffMs / 1000));
  const minutes = Math.floor(diffSec / 60);
  const seconds = diffSec % 60;
  if (minutes === 0) {
    return `${seconds}s`;
  }
  return `${minutes}m ${seconds}s`;
};
