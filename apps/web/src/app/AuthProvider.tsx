import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";
import {
  SESSION_EXPIRED_EVENT,
  generatedApi,
  asRecord,
  booleanValue,
  nested,
  numberValue,
  stringList,
  stringValue,
  type ApiRecord,
} from "../api/client";

type AuthContextValue = {
  user?: ApiRecord;
  organisation?: ApiRecord;
  permissions: Set<string>;
  roles: string[];
  authenticated: boolean;
  loading: boolean;
  error: unknown;
  oidcEnabled: boolean;
  localEnabled: boolean;
  sseEnabled: boolean;
  pollingFallbackSeconds: number;
  maximumFirmwareSizeMb: number;
  refresh: () => Promise<unknown>;
  clearSession: () => void;
  can: (permission: string) => boolean;
  canAny: (...permissions: string[]) => boolean;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

const currentSession = generatedApi.currentIdentity;

export function AuthProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [sessionCleared, setSessionCleared] = useState(false);
  const configQuery = useQuery({
    queryKey: ["auth", "config"],
    queryFn: generatedApi.authenticationConfig,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const meQuery = useQuery({
    queryKey: ["auth", "me"],
    queryFn: currentSession,
    enabled: !sessionCleared,
    staleTime: 30_000,
    retry: false,
  });
  const clearSession = useCallback(() => {
    setSessionCleared(true);
    const isPrivateQuery = (query: { queryKey: readonly unknown[] }) =>
      query.queryKey[0] !== "auth" || query.queryKey[1] !== "config";
    void queryClient.cancelQueries({ predicate: isPrivateQuery });
    queryClient.removeQueries({ predicate: isPrivateQuery });
  }, [queryClient]);
  const refresh = useCallback(async () => {
    setSessionCleared(false);
    return queryClient.fetchQuery({
      queryKey: ["auth", "me"],
      queryFn: currentSession,
      staleTime: 0,
    });
  }, [queryClient]);

  useEffect(() => {
    window.addEventListener(SESSION_EXPIRED_EVENT, clearSession);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, clearSession);
  }, [clearSession]);

  const value = useMemo<AuthContextValue>(() => {
    const root = sessionCleared ? undefined : asRecord(meQuery.data);
    const user = asRecord(root?.principal);
    const organisation = asRecord(root?.organisation);
    const permissions = new Set([
      ...stringList(root, "permissions"),
      ...stringList(asRecord(root?.authorization) ?? asRecord(root?.authorisation), "permissions"),
    ]);
    const roles = [
      ...stringList(root, "roles"),
      ...(stringValue(root, "membership_role") ? [stringValue(root, "membership_role")!] : []),
    ];
    const isOwner = roles.some((role) =>
      ["OWNER", "ORGANISATION_OWNER", "ADMIN"].includes(role.toUpperCase()),
    );
    const can = (permission: string) =>
      isOwner ||
      permissions.has("*") ||
      permissions.has(permission) ||
      permissions.has(permission.replace(":", "."));
    const config = asRecord(configQuery.data);
    const liveConfig = nested(nested(config, "web"), "live_updates");
    const uploadConfig = nested(nested(config, "web"), "uploads");
    return {
      user,
      organisation,
      permissions,
      roles,
      authenticated: Boolean(user),
      loading: meQuery.isLoading,
      error: meQuery.error,
      oidcEnabled: Boolean(config?.oidc_enabled ?? config?.oidc ?? false),
      localEnabled: config?.local_enabled !== false,
      sseEnabled: booleanValue(liveConfig, "sse_enabled", true),
      pollingFallbackSeconds: Math.max(2, numberValue(liveConfig, "polling_fallback_seconds", 5)),
      maximumFirmwareSizeMb: Math.max(
        1,
        numberValue(uploadConfig, "maximum_firmware_size_mb", 100),
      ),
      refresh,
      clearSession,
      can,
      canAny: (...required) => required.some(can),
    };
  }, [
    clearSession,
    configQuery.data,
    meQuery.data,
    meQuery.error,
    meQuery.isLoading,
    refresh,
    sessionCleared,
  ]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
