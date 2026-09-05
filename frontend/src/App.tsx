import { useEffect } from "react";
import { Route, BrowserRouter, Routes, useNavigate, useSearchParams } from "react-router-dom";
import LandingPage from "./routes/landing/LandingPage";
import BoatApp from "./routes/boat/BoatApp";
import ConsoleApp from "./routes/console/ConsoleApp";
import { isMobileDevice } from "@shared/device";

/**
 * Adaptive root route:
 * Automatically routes mobile users directly to the Boat UI (/boat) unless they
 * explicitly request the landing page (via ?landing=true or a prior opt-in in sessionStorage).
 * Desktop users remain on the Landing Page.
 */
function HomeRoute() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const forceLanding =
    searchParams.get("landing") === "true" ||
    (typeof window !== "undefined" &&
      sessionStorage.getItem("foreshore_skip_mobile_redirect") === "1");

  const shouldRedirectToBoat = !forceLanding && isMobileDevice();

  useEffect(() => {
    if (forceLanding) {
      sessionStorage.setItem("foreshore_skip_mobile_redirect", "1");
      return;
    }
    if (shouldRedirectToBoat) {
      navigate("/boat", { replace: true });
    }
  }, [navigate, forceLanding, shouldRedirectToBoat]);

  if (shouldRedirectToBoat) {
    return null;
  }

  return <LandingPage />;
}

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
        <Route path="/" element={<HomeRoute />} />
        <Route path="/boat/*" element={<BoatApp />} />
        <Route path="/console/*" element={<ConsoleApp />} />
      </Routes>
    </BrowserRouter>
  );
}

