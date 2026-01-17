import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchSettings, updateSettings } from "./api";
import type { SettingsResponse, SettingsUpdatePayload } from "@/types/settings";
import { useAuthStore } from "@/features/auth/store";

export const useSettings = () => {
  const queryClient = useQueryClient();
  const accessToken = useAuthStore((state) => state.accessToken);
  const isAuthenticated = Boolean(accessToken);

  const {
    data,
    isError,
    isLoading,
    status,
    refetch,
  } = useQuery({
    queryKey: ["settings"],
    queryFn: fetchSettings,
    enabled: isAuthenticated,
    staleTime: 60_000,
  });

  const { mutateAsync: mutateSettings, isPending: isUpdating } = useMutation({
    mutationFn: (payload: SettingsUpdatePayload) => updateSettings(payload),
    onSuccess: (next: SettingsResponse) => {
      queryClient.setQueryData(["settings"], next);
    },
  });

  return {
    data,
    isError,
    isLoading,
    status,
    refetch,
    updateSettings: mutateSettings,
    isUpdating,
  };
};
