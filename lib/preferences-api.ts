export type FontScale = "small" | "standard" | "large";

export interface UserPreferences {
  fontScale: FontScale;
  pdfZoom: number;
  leftPanelOpen: boolean;
  assistantPanelOpen: boolean;
  translationLanguage: string;
  arxivSearchEnabled: boolean;
}

export interface CurrentUser {
  id: string;
  email: string;
  displayName: string;
  role: "admin" | "user";
  active: boolean;
  mustChangePassword: boolean;
  preferences: UserPreferences;
}

export interface PreferencesUpdate extends Partial<UserPreferences> {
  displayName?: string;
}

export class SessionExpiredError extends Error {}

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";

const defaultPreferences: UserPreferences = {
  fontScale: "standard",
  pdfZoom: 100,
  leftPanelOpen: true,
  assistantPanelOpen: true,
  translationLanguage: "zh-CN",
  arxivSearchEnabled: false,
};

function readCookie(name: string): string {
  if (typeof document === "undefined") return "";
  const item = document.cookie.split("; ").find((cookie) => cookie.startsWith(`${name}=`));
  return item ? decodeURIComponent(item.slice(name.length + 1)) : "";
}

function mutationHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const csrf = readCookie("paperleaf_csrf");
  return csrf ? { ...extra, "X-CSRF-Token": csrf } : extra;
}

async function errorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json() as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
    if (body.detail && typeof body.detail === "object") {
      const message = (body.detail as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return message;
    }
  } catch {
    // 非 JSON 错误沿用面向用户的默认提示。
  }
  return fallback;
}

function mapPreferences(raw: Record<string, unknown> | undefined): UserPreferences {
  return {
    fontScale: raw?.font_scale === "small" || raw?.font_scale === "large" ? raw.font_scale : "standard",
    pdfZoom: Number(raw?.pdf_zoom ?? defaultPreferences.pdfZoom),
    leftPanelOpen: raw?.left_panel_open !== false,
    assistantPanelOpen: raw?.assistant_panel_open !== false,
    translationLanguage: String(raw?.translation_language ?? defaultPreferences.translationLanguage),
    arxivSearchEnabled: raw?.arxiv_search_enabled === true,
  };
}

function mapCurrentUser(raw: Record<string, unknown>): CurrentUser {
  const email = String(raw.email ?? "");
  return {
    id: String(raw.id ?? ""),
    email,
    displayName: String(raw.display_name ?? "").trim() || email.split("@")[0] || "研究者",
    role: raw.role === "admin" ? "admin" : "user",
    active: raw.active !== false,
    mustChangePassword: raw.must_change_password === true,
    preferences: mapPreferences(raw.preferences as Record<string, unknown> | undefined),
  };
}

export function applyFontScale(scale: FontScale): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.fontScale = scale;
}

export async function getCurrentUser(): Promise<CurrentUser> {
  const response = await fetch(`${API_BASE_URL}/auth/me`, { credentials: "include" });
  if (!response.ok) {
    const message = await errorMessage(response, "账户信息读取失败");
    if (response.status === 401) throw new SessionExpiredError(message);
    throw new Error(message);
  }
  return mapCurrentUser(await response.json() as Record<string, unknown>);
}

export async function getUserPreferences(): Promise<PreferencesUpdate & UserPreferences> {
  const response = await fetch(`${API_BASE_URL}/users/me/preferences`, { credentials: "include" });
  if (!response.ok) throw new Error(await errorMessage(response, "个人设置读取失败"));
  const raw = await response.json() as Record<string, unknown>;
  return { displayName: String(raw.display_name ?? ""), ...mapPreferences(raw) };
}

export async function updateUserPreferences(input: PreferencesUpdate): Promise<PreferencesUpdate & UserPreferences> {
  const response = await fetch(`${API_BASE_URL}/users/me/preferences`, {
    method: "PATCH",
    credentials: "include",
    headers: mutationHeaders({ "content-type": "application/json" }),
    body: JSON.stringify({
      ...(input.displayName === undefined ? {} : { display_name: input.displayName }),
      ...(input.fontScale === undefined ? {} : { font_scale: input.fontScale }),
      ...(input.pdfZoom === undefined ? {} : { pdf_zoom: input.pdfZoom }),
      ...(input.leftPanelOpen === undefined ? {} : { left_panel_open: input.leftPanelOpen }),
      ...(input.assistantPanelOpen === undefined ? {} : { assistant_panel_open: input.assistantPanelOpen }),
      ...(input.translationLanguage === undefined ? {} : { translation_language: input.translationLanguage }),
      ...(input.arxivSearchEnabled === undefined ? {} : { arxiv_search_enabled: input.arxivSearchEnabled }),
    }),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "个人设置保存失败"));
  const raw = await response.json() as Record<string, unknown>;
  return { displayName: String(raw.display_name ?? ""), ...mapPreferences(raw) };
}

export async function logout(): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/auth/logout`, {
    method: "POST",
    credentials: "include",
    headers: mutationHeaders(),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "退出登录失败"));
}

export async function updatePassword(currentPassword: string, newPassword: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/auth/change-password`, {
    method: "POST",
    credentials: "include",
    headers: mutationHeaders({ "content-type": "application/json" }),
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "密码修改失败"));
}

export const demoCurrentUser: CurrentUser = {
  id: "demo",
  email: "demo@paperleaf.local",
  displayName: "演示研究员",
  role: "admin",
  active: true,
  mustChangePassword: false,
  preferences: defaultPreferences,
};
