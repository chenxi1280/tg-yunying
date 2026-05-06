/// <reference types="vite/client" />

declare module 'lucide-react' {
  import type { ComponentType, SVGProps } from 'react';

  export type LucideIcon = ComponentType<SVGProps<SVGSVGElement> & { size?: number | string }>;

  export const Activity: LucideIcon;
  export const Archive: LucideIcon;
  export const Bot: LucideIcon;
  export const CheckCircle2: LucideIcon;
  export const ClipboardCheck: LucideIcon;
  export const Database: LucideIcon;
  export const LayoutDashboard: LucideIcon;
  export const LockKeyhole: LucideIcon;
  export const MessageSquareText: LucideIcon;
  export const RefreshCcw: LucideIcon;
  export const Send: LucideIcon;
  export const ShieldAlert: LucideIcon;
  export const Smartphone: LucideIcon;
  export const Users: LucideIcon;
}
