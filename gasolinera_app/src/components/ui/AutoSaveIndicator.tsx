"use client";

import clsx from "classnames";

import type { AutoSaveStatus } from "@/hooks/useAutoSaveFeedback";

interface AutoSaveIndicatorProps {
  status: AutoSaveStatus;
  labels: {
    saving: string;
    saved: string;
    error: string;
  };
  message?: string;
}

const indicatorStyles: Record<Exclude<AutoSaveStatus, "idle">, string> = {
  saving: "bg-amber-100 text-amber-700 border-amber-200",
  saved: "bg-emerald-100 text-emerald-700 border-emerald-200",
  error: "bg-rose-100 text-rose-700 border-rose-200",
};

const AutoSaveIndicator = ({ status, labels, message }: AutoSaveIndicatorProps) => {
  if (status === "idle") {
    return null;
  }

  const text = message ?? labels[status];
  return (
    <div
      className={clsx(
        "inline-flex items-center gap-1 rounded-full border px-3 py-1 text-xs font-medium",
        indicatorStyles[status]
      )}
      role={status === "error" ? "alert" : "status"}
    >
      {status === "saving" ? (
        <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
      ) : null}
      {status === "saved" ? (
        <span className="h-2 w-2 rounded-full bg-emerald-500" />
      ) : null}
      {status === "error" ? (
        <span className="h-2 w-2 rounded-full bg-rose-600" />
      ) : null}
      <span>{text}</span>
    </div>
  );
};

export default AutoSaveIndicator;
