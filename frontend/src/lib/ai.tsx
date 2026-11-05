import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "./api";

/** The AI works in the background and keeps going if you leave the Review screen, so its state
    lives here, above the router, where the sidebar can also see it. */

export interface AiResult {
  asked: number;
  agreed: number;
  other: number;
  unsure: number;
  errors: number;
  tokens: number;
  seconds: number;
  note: string | null;
}

interface AiState {
  status: "idle" | "running" | "done";
  total: number;
  done: number;
  activeIds: number[];
  queuedIds: number[];
  touched: number[];
  result: AiResult | null;
  unseen: number;
}

interface AiApi extends AiState {
  run: (ids: number[], everything: boolean, redo?: boolean) => Promise<void>;
  dismiss: () => void;
  markSeen: () => void;
  clearTouched: () => void;
}

const BATCH = 10; // the server answers at most 10 payments per request
const CAP = 50; // and allows a handful of requests per person every ten minutes

const IDLE: AiState = {
  status: "idle",
  total: 0,
  done: 0,
  activeIds: [],
  queuedIds: [],
  touched: [],
  result: null,
  unseen: 0,
};
const Ctx = createContext<AiApi | null>(null);

type RerankReply = {
  asked: number;
  agreed_with_rules: number;
  picked_another: number;
  unsure: number;
  errors: number;
  tokens: number;
  seconds: number;
  notes: string[];
};

export function AiProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [s, setS] = useState<AiState>(IDLE);
  const busy = useRef(false);

  const run = useCallback(
    async (all: number[], everything: boolean, redo = false) => {
      if (busy.current || all.length === 0) return;
      busy.current = true;
      const ids = all.slice(0, CAP);
      const result: AiResult = {
        asked: 0,
        agreed: 0,
        other: 0,
        unsure: 0,
        errors: 0,
        tokens: 0,
        seconds: 0,
        note: null,
      };
      setS({ ...IDLE, status: "running", total: ids.length, queuedIds: ids });
      const touched: number[] = [];
      try {
        for (let i = 0; i < ids.length; i += BATCH) {
          const batch = ids.slice(i, i + BATCH);
          setS((p) => ({
            ...p,
            activeIds: batch,
            queuedIds: ids.slice(i + BATCH),
          }));
          try {
            const r = await api<RerankReply>("/review/rerank", {
              method: "POST",
              body: { group_ids: batch, everything, limit: BATCH, redo },
            });
            result.asked += r.asked;
            result.agreed += r.agreed_with_rules;
            result.other += r.picked_another;
            result.unsure += r.unsure;
            result.errors += r.errors;
            result.tokens += r.tokens ?? 0;
            result.seconds += r.seconds ?? 0;
            if (r.notes?.length) result.note = r.notes[0];
            touched.push(...batch);
            await qc.invalidateQueries({ queryKey: ["review"] });
            setS((p) => ({
              ...p,
              done: Math.min(ids.length, i + batch.length),
              touched: [...touched],
            }));
            if (r.errors > 0 && r.notes?.length) break; // the service is down or limited
          } catch (e) {
            result.note =
              e instanceof Error ? e.message : "The AI request failed.";
            break;
          }
        }
      } finally {
        busy.current = false;
        setS((p) => ({
          ...p,
          status: "done",
          activeIds: [],
          queuedIds: [],
          result,
          unseen: result.asked,
        }));
      }
    },
    [qc],
  );

  const value = useMemo<AiApi>(
    () => ({
      ...s,
      run,
      dismiss: () => setS(IDLE),
      markSeen: () => setS((p) => (p.unseen ? { ...p, unseen: 0 } : p)),
      clearTouched: () =>
        setS((p) => (p.touched.length ? { ...p, touched: [] } : p)),
    }),
    [s, run],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAi(): AiApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAi outside AiProvider");
  return v;
}

export interface AiStatus {
  configured: boolean;
  provider: string;
  model: string | null;
}

/** Whether the server has an AI key, and which model it would use. No call to the provider is made. */
export const useAiStatus = () =>
  useQuery({
    queryKey: ["review", "ai-status"],
    queryFn: () => api<AiStatus>("/review/ai-status"),
    staleTime: 60_000,
  });
