import type { InvoiceDataSource } from "../../lib/api/types";

type SourceFilterProps = {
  dataSource: InvoiceDataSource;
  onChange: (source: InvoiceDataSource) => void;
};

export function SourceFilter({ dataSource, onChange }: SourceFilterProps) {
  return (
    <div className="source-filter" role="group" aria-label="Data source">
      <span className="source-filter__label">Source</span>
      <div className="source-filter__options">
        <button
          type="button"
          className={
            dataSource === "bills"
              ? "source-filter__btn source-filter__btn--active"
              : "source-filter__btn"
          }
          onClick={() => onChange("bills")}
          aria-pressed={dataSource === "bills"}
        >
          Bills
        </button>
        <button
          type="button"
          className={
            dataSource === "xtract"
              ? "source-filter__btn source-filter__btn--active"
              : "source-filter__btn"
          }
          onClick={() => onChange("xtract")}
          aria-pressed={dataSource === "xtract"}
        >
          XtractBills
        </button>
        <button
          type="button"
          className={
            dataSource === "btcbills"
              ? "source-filter__btn source-filter__btn--active"
              : "source-filter__btn"
          }
          onClick={() => onChange("btcbills")}
          aria-pressed={dataSource === "btcbills"}
        >
          BTCBills
        </button>
      </div>
    </div>
  );
}
