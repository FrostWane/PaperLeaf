"use client";

import { useEffect, useSyncExternalStore } from "react";
import { applyFontScale, demoCurrentUser, getCurrentUser, SessionExpiredError, type CurrentUser } from "@/lib/preferences-api";

type UserMode = "demo" | "real";
type UserStatus = "idle" | "loading" | "ready" | "error";

interface CurrentUserSnapshot {
  mode: UserMode | null;
  user: CurrentUser | null;
  status: UserStatus;
  error: string;
  unauthorized: boolean;
}

const emptySnapshot: CurrentUserSnapshot = { mode: null, user: null, status: "idle", error: "", unauthorized: false };
let snapshot = emptySnapshot;
let inFlight: Promise<CurrentUser> | null = null;
let requestGeneration = 0;
const listeners = new Set<() => void>();

function emit(next: CurrentUserSnapshot) {
  snapshot = next;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return snapshot;
}

export async function ensureCurrentUser(mode: UserMode): Promise<CurrentUser> {
  if (mode === "demo") {
    if (snapshot.mode !== "demo" || snapshot.user?.id !== demoCurrentUser.id) {
      requestGeneration += 1;
      inFlight = null;
      applyFontScale(demoCurrentUser.preferences.fontScale);
      emit({ mode, user: demoCurrentUser, status: "ready", error: "", unauthorized: false });
    }
    return demoCurrentUser;
  }
  if (snapshot.mode === "real" && snapshot.status === "ready" && snapshot.user) return snapshot.user;
  if (inFlight) return inFlight;
  const generation = ++requestGeneration;
  emit({ mode, user: null, status: "loading", error: "", unauthorized: false });
  inFlight = getCurrentUser()
    .then((user) => {
      if (generation !== requestGeneration) return user;
      applyFontScale(user.preferences.fontScale);
      emit({ mode, user, status: "ready", error: "", unauthorized: false });
      return user;
    })
    .catch((reason: unknown) => {
      if (generation !== requestGeneration) throw reason;
      const message = reason instanceof Error ? reason.message : "账户信息读取失败";
      emit({ mode, user: null, status: "error", error: message, unauthorized: reason instanceof SessionExpiredError });
      throw reason;
    })
    .finally(() => { if (generation === requestGeneration) inFlight = null; });
  return inFlight;
}

export function clearCurrentUserState() {
  requestGeneration += 1;
  inFlight = null;
  emit(emptySnapshot);
}

export function resetCurrentUserStateForTests() {
  clearCurrentUserState();
}

export function useCurrentUserState(mode: UserMode): CurrentUserSnapshot {
  const current = useSyncExternalStore(subscribe, getSnapshot, () => emptySnapshot);
  useEffect(() => { void ensureCurrentUser(mode).catch(() => undefined); }, [mode]);
  return current.mode === mode ? current : { ...emptySnapshot, mode, status: "loading" };
}

export function CurrentUserProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    function syncProfile(event: Event) {
      const detail = (event as CustomEvent<{ displayName?: string; preferences?: CurrentUser["preferences"] }>).detail;
      if (!detail || snapshot.mode !== "real" || !snapshot.user) return;
      const user = {
        ...snapshot.user,
        ...(detail.displayName ? { displayName: detail.displayName } : {}),
        ...(detail.preferences ? { preferences: detail.preferences } : {}),
      };
      if (detail.preferences) applyFontScale(detail.preferences.fontScale);
      emit({ ...snapshot, user });
    }
    window.addEventListener("paperleaf:profile-updated", syncProfile);
    return () => window.removeEventListener("paperleaf:profile-updated", syncProfile);
  }, []);
  return children;
}
