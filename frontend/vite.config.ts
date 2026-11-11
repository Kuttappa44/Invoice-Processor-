import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // Load environment variables from .env files
  // Use "." as root directory (current directory)
  const env = loadEnv(mode, ".", "");
  
  // Use base path from environment or default to root
  // For Jarvis Labs: set VITE_BASE_PATH=/proxy/8005/ in .env before building
  const base = env.VITE_BASE_PATH || "/";

  return {
    base: base,
    plugins: [react()],
    server: {
      port: Number(env.VITE_DEV_PORT || 5173),
      host: "localhost", // Listen on localhost for local development
      strictPort: false,
      // Proxy API requests to backend during development
      proxy: {
        "/api": {
          target: env.VITE_API_BASE_URL || "http://localhost:8005",
          changeOrigin: true,
          secure: false,
        },
      },
    },
    css: {
      preprocessorOptions: {
        scss: {
          charset: false,
        },
      },
    },
  };
});

