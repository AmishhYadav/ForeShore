import { Route, BrowserRouter, Routes } from "react-router-dom";
import LandingPage from "./routes/landing/LandingPage";
import BoatApp from "./routes/boat/BoatApp";
import ConsoleApp from "./routes/console/ConsoleApp";

/**
 * "Two surfaces, one agent core" (CLAUDE.md's central architectural claim) means one
 * React app, one router, one shared/ contract layer — /boat and /console are two route
 * trees, not two separate projects. Do not split this into two Vite apps.
 *
 * The landing page at "/" is a pure presentation surface with no backend calls — it
 * exists to sell the project before the user enters the tool at /boat or /console.
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/boat/*" element={<BoatApp />} />
        <Route path="/console/*" element={<ConsoleApp />} />
      </Routes>
    </BrowserRouter>
  );
}
