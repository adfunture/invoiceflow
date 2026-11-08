import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Im Betrieb liefert FastAPI den Build aus (gleiche Domain → Session-Cookie + CSRF ohne CORS).
// In der Entwicklung leitet Vite /api an das lokale Backend weiter.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8091", "/health": "http://localhost:8091" },
  },
  build: { outDir: "dist", sourcemap: false },
});
