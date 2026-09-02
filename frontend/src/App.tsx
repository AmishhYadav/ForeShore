import { Navigate, Route, BrowserRouter, Routes } from "react-router-dom";
import BoatApp from "./routes/boat/BoatApp";
import ConsoleApp from "./routes/console/ConsoleApp";

/**
 * "Two surfaces, one agent core" (CLAUDE.md's central architectural claim) means one
 * React app, one router, one shared/ contract layer — /boat and /console are two route
 * trees, not two separate projects. Do not split this into two Vite apps.
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/boat" replace />} />
        <Route path="/boat/*" element={<BoatApp />} />
        <Route path="/console/*" element={<ConsoleApp />} />
      </Routes>
    </BrowserRouter>
  );
}
