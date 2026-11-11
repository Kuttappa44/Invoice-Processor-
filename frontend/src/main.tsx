import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// Debug logging
console.log("🚀 Starting Invoice Processor Frontend...");
console.log("📍 API Base URL:", import.meta.env.VITE_API_BASE_URL ?? "/proxy/8005");
console.log("🌐 Current URL:", window.location.href);

// Error boundary for debugging
const rootElement = document.getElementById("root");
if (!rootElement) {
  const error = new Error("Root element not found. Make sure index.html has <div id='root'></div>");
  console.error("❌", error);
  document.body.innerHTML = `
    <div style="padding: 20px; font-family: sans-serif; color: red;">
      <h1>❌ Critical Error</h1>
      <p>${error.message}</p>
    </div>
  `;
  throw error;
}

console.log("✅ Root element found, rendering app...");

try {
  const root = ReactDOM.createRoot(rootElement);
  root.render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
  console.log("✅ App rendered successfully");
} catch (error) {
  console.error("❌ Failed to render app:", error);
  rootElement.innerHTML = `
    <div style="padding: 20px; font-family: sans-serif; background: #fee; border: 2px solid #f00; border-radius: 8px; margin: 20px;">
      <h1 style="color: #c00;">❌ Error Loading App</h1>
      <p><strong>Error:</strong> ${error instanceof Error ? error.message : String(error)}</p>
      <details style="margin-top: 10px;">
        <summary style="cursor: pointer; color: #0066cc;">Show Stack Trace</summary>
        <pre style="background: #fff; padding: 10px; border: 1px solid #ccc; overflow: auto; margin-top: 10px;">${error instanceof Error ? error.stack : String(error)}</pre>
      </details>
      <p style="margin-top: 20px; font-size: 12px; color: #666;">
        Check the browser console (F12) for more details.
      </p>
    </div>
  `;
}

