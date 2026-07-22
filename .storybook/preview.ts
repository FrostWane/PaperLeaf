import type { Preview } from "@storybook/react-vite";
import "../app/globals.css";

const preview: Preview = { parameters: { layout: "fullscreen", a11y: { test: "error" } } };
export default preview;
