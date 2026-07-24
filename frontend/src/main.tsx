import React from "react";
import { createRoot } from "react-dom/client";
import "./globals.css";
import Dashboard from "./page";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Dashboard />
  </React.StrictMode>,
);
