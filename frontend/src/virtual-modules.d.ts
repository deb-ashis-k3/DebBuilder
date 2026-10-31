declare module "virtual:used-icons" {
  import type { ComponentType } from "react";
  import type { LucideProps } from "lucide-react";
  const icons: Record<string, ComponentType<LucideProps>>;
  export default icons;
}
