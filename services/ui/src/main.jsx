import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

// Kiosk: no context menu on long-press, no accidental text selection.
window.addEventListener("contextmenu", (e) => e.preventDefault());

createRoot(document.getElementById("root")).render(<App />);
