import { button, cx } from "@/shared/lib/classes";
import { useTheme } from "./theme";

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const next = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
  return (
    <button type="button" id="themeToggle" className={cx(button, "self-end")} title={next} aria-label={next} onClick={toggle}>
      {theme === "dark" ? "☀️" : "🌙"}
    </button>
  );
}
