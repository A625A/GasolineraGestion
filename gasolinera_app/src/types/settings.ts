export type LanguageOption = "es" | "en";

export interface PersonalInfo {
  fullName: string;
  role: string;
  location?: string;
}

export interface ContactInfo {
  phone: string;
  email: string;
  emailVerified: boolean;
}

export interface NotificationPreferences {
  phone: boolean;
  gmail: boolean;
  inApp: boolean;
}

export interface PredictiveSettings {
  confidenceThreshold: number;
}

export interface SettingsResponse {
  language: LanguageOption;
  personalInfo: PersonalInfo;
  contact: ContactInfo;
  notificationPreferences: NotificationPreferences;
  predictive: PredictiveSettings;
  updatedAt: string;
}

export type SettingsUpdatePayload = Partial<SettingsResponse> & {
  personalInfo?: Partial<PersonalInfo>;
  contact?: Partial<ContactInfo>;
  notificationPreferences?: Partial<NotificationPreferences>;
  predictive?: Partial<PredictiveSettings>;
};
