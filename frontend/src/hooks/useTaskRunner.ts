import { useCallback, useMemo, useRef, useState } from "react";
import {
  getTask,
  postMatchMasterTask,
  postProcessTask,
} from "../lib/api/client";
import type { TaskResponse, TaskStatus } from "../lib/api/types";

export type { TaskStatus };
export interface TaskRecord {
  task_id: string;
  status: TaskStatus;
  started_at: string;
  finished_at?: string;
  logs: string;
  error?: string | null;
  excel_link?: string | null;
}

type TaskTarget = "process" | "match-master";

interface ProcessTaskOptions {
  fromDate?: string;
  toDate?: string;
  forceReplace?: boolean;
}

export function useTaskRunner() {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const pollingRefs = useRef<Record<string, boolean>>({});

  const stopPolling = useCallback((taskId: string) => {
    pollingRefs.current[taskId] = false;
  }, []);

  const upsertTask = useCallback((incoming: TaskResponse) => {
    setTasks((prev) => {
      const existingIndex = prev.findIndex(
        (task) => task.task_id === incoming.task_id,
      );
      if (existingIndex === -1) {
        return [
          {
            ...incoming,
            status: incoming.status ?? "running",
            error: incoming.error ?? null,
            excel_link: incoming.excel_link ?? null,
          },
          ...prev,
        ];
      }

      const updated = [...prev];
      updated[existingIndex] = {
        ...updated[existingIndex],
        ...incoming,
        error: incoming.error ?? null,
        excel_link: incoming.excel_link ?? null,
      };
      return updated;
    });
  }, []);

  const pollTask = useCallback(
    async (taskId: string) => {
      pollingRefs.current[taskId] = true;

      const tick = async () => {
        if (!pollingRefs.current[taskId]) {
          return;
        }
        try {
          const { data } = await getTask(taskId);
          upsertTask(data);
          if (data.status !== "running") {
            stopPolling(taskId);
            return;
          }
        } catch (error) {
          stopPolling(taskId);
        }
        if (pollingRefs.current[taskId]) {
          setTimeout(tick, 2000);
        }
      };

      tick();
    },
    [stopPolling, upsertTask],
  );

  const startTask = useCallback(
    async (target: TaskTarget, options?: ProcessTaskOptions) => {
      const {
        data: { task_id: taskId },
      } =
        target === "process"
          ? await postProcessTask({
              from_date: options?.fromDate || null,
              to_date: options?.toDate || null,
              force_replace: options?.forceReplace ?? false,
            })
          : await postMatchMasterTask();

      const placeholder: TaskResponse = {
        task_id: taskId,
        status: "running",
        started_at: new Date().toISOString(),
        logs: "",
      };
      upsertTask(placeholder);
      pollTask(taskId);
      return taskId;
    },
    [pollTask, upsertTask],
  );

  const isRunning = useMemo(
    () => tasks.some((task) => task.status === "running"),
    [tasks],
  );

  return {
    tasks,
    isRunning,
    startTask,
  };
}
