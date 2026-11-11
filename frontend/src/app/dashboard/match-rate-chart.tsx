import { motion } from "framer-motion";
import { TrendingUp } from "lucide-react";
import {
  CartesianGrid,
  LabelList,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { MatchRateTrendPoint } from "../../lib/api/types";
import { formatChartDate } from "../../lib/dates";

const CHART_LINE_COLOR = "#dc2626";

const MatchRateTooltip = ({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { payload: MatchRateTrendPoint }[];
  label?: string;
}) => {
  if (!active || !payload?.length || !label) return null;
  const point = payload[0].payload;
  return (
    <div className="match-rate-chart__tooltip">
      <div className="match-rate-chart__tooltip-date">
        {formatChartDate(label)}
      </div>
      <div className="match-rate-chart__tooltip-rate">
        {point.match_rate}%
      </div>
      <div className="match-rate-chart__tooltip-detail">
        {point.matched} matched · {point.unmatched} unmatched
      </div>
    </div>
  );
};

const renderMatchRateLabel = (
  props: {
    x?: number | string;
    y?: number | string;
    value?: number | string;
    index?: number;
  },
  points: MatchRateTrendPoint[],
) => {
  const { x, y, value, index } = props;
  if (x == null || y == null || value == null || index == null) return null;
  const point = points[index];
  if (!point || (point.matched === 0 && point.unmatched === 0)) return null;
  return (
    <text
      x={Number(x)}
      y={Number(y) - 12}
      textAnchor="middle"
      fill="#374151"
      fontSize={11}
      fontWeight={600}
    >
      {value}%
    </text>
  );
};

type MatchRateChartProps = {
  graphFromDate: string;
  graphToDate: string;
  graphPoints: MatchRateTrendPoint[];
  graphLoading: boolean;
  graphError: string | null;
  onFromDate: (value: string) => void;
  onToDate: (value: string) => void;
  onLoad: () => void;
};

export function MatchRateChart({
  graphFromDate,
  graphToDate,
  graphPoints,
  graphLoading,
  graphError,
  onFromDate,
  onToDate,
  onLoad,
}: MatchRateChartProps) {
  return (
    <motion.section
      className="card match-rate-chart-card"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.28 }}
    >
      <div className="match-rate-chart__header">
        <div className="match-rate-chart__title-wrap">
          <TrendingUp size={22} className="match-rate-chart__icon" />
          <h2 className="card__title match-rate-chart__title">
            Match rate trend
          </h2>
        </div>
        <div className="match-rate-chart__toolbar">
          <label className="stats-filter__field">
            <span>From</span>
            <input
              type="date"
              value={graphFromDate}
              onChange={(e) => onFromDate(e.target.value)}
            />
          </label>
          <label className="stats-filter__field">
            <span>To</span>
            <input
              type="date"
              value={graphToDate}
              onChange={(e) => onToDate(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="button button--gradient"
            onClick={onLoad}
            disabled={graphLoading}
          >
            <TrendingUp size={16} />
            {graphLoading ? "Loading…" : "Generate Graph"}
          </button>
        </div>
      </div>

      {graphError && (
        <p className="match-rate-chart__error" role="alert">
          {graphError}
        </p>
      )}

      <div className="match-rate-chart__plot">
        {graphLoading && graphPoints.length === 0 ? (
          <p className="match-rate-chart__empty">Loading chart…</p>
        ) : graphPoints.length === 0 ? (
          <p className="match-rate-chart__empty">
            No data for this date range.
          </p>
        ) : (
          <ResponsiveContainer width="100%" height={360}>
            <LineChart
              data={graphPoints}
              margin={{ top: 32, right: 24, left: 8, bottom: 8 }}
            >
              <CartesianGrid
                stroke="#d1d5db"
                strokeDasharray="0"
                vertical
                horizontal
              />
              <XAxis
                dataKey="date"
                tickFormatter={formatChartDate}
                tick={{ fill: "#374151", fontSize: 12 }}
                axisLine={{ stroke: "#9ca3af" }}
                tickLine={{ stroke: "#9ca3af" }}
                interval="preserveStartEnd"
                minTickGap={28}
                label={{
                  value: "Date",
                  position: "insideBottom",
                  offset: -4,
                  fill: "#374151",
                  fontSize: 13,
                  fontWeight: 600,
                }}
              />
              <YAxis
                domain={[0, 100]}
                tickCount={6}
                tickFormatter={(v) => `${v}%`}
                tick={{ fill: "#374151", fontSize: 12 }}
                axisLine={{ stroke: "#9ca3af" }}
                tickLine={{ stroke: "#9ca3af" }}
                label={{
                  value: "Match rate (%)",
                  angle: -90,
                  position: "insideLeft",
                  fill: "#374151",
                  fontSize: 13,
                  fontWeight: 600,
                }}
              />
              <Tooltip content={<MatchRateTooltip />} />
              <Line
                type="linear"
                dataKey="match_rate"
                stroke={CHART_LINE_COLOR}
                strokeWidth={3}
                dot={{
                  r: 6,
                  fill: CHART_LINE_COLOR,
                  stroke: "#fff",
                  strokeWidth: 2,
                }}
                activeDot={{
                  r: 8,
                  fill: CHART_LINE_COLOR,
                  stroke: "#fff",
                  strokeWidth: 2,
                }}
                connectNulls
              >
                <LabelList
                  content={(props) =>
                    renderMatchRateLabel(
                      props as {
                        x?: number | string;
                        y?: number | string;
                        value?: number | string;
                        index?: number;
                      },
                      graphPoints,
                    )
                  }
                />
              </Line>
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </motion.section>
  );
}
