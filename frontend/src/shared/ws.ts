/**
 * WS /ws/alerts client. Both /boat (its own vessel's alerts) and /console (the whole
 * fleet) subscribe through this same class — the "same agent core, different renderer"
 * claim extends to the transport, not just the request path.
 */
import { API_BASE } from "./api";
import type { WsClientMessage, WsServerMessage } from "./types";

export type WsListener = (msg: WsServerMessage) => void;

export class AlertSocket {
  private socket: WebSocket | null = null;
  private listeners = new Set<WsListener>();
  private reconnectDelayMs = 1000;
  private closedByUser = false;
  private vesselIds: string[] | null = null;

  connect(): void {
    this.closedByUser = false;
    const url = `${API_BASE.replace(/^http/, "ws")}/ws/alerts`;
    const socket = new WebSocket(url);
    this.socket = socket;

    socket.onmessage = (event) => {
      let msg: WsServerMessage;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      for (const listener of this.listeners) listener(msg);
    };

    socket.onopen = () => {
      this.reconnectDelayMs = 1000;
      if (this.vesselIds) this.subscribe(this.vesselIds);
    };

    socket.onclose = () => {
      if (this.closedByUser) return;
      // Backoff up to 15s — offline mode is a first-class state here (PLAN.md's "No
      // signal" toggle), not an error to surface loudly; the geofence check itself
      // still runs client-side without this socket.
      setTimeout(() => this.connect(), this.reconnectDelayMs);
      this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, 15000);
    };
  }

  disconnect(): void {
    this.closedByUser = true;
    this.socket?.close();
    this.socket = null;
  }

  onMessage(listener: WsListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private send(msg: WsClientMessage): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(msg));
    }
  }

  ack(alertId: string, by: string): void {
    this.send({ type: "ack", alert_id: alertId, by });
  }

  /** Empty/omitted vesselIds = subscribe to every vessel (the console's default). */
  subscribe(vesselIds: string[]): void {
    this.vesselIds = vesselIds;
    this.send({ type: "subscribe", vessel_ids: vesselIds });
  }
}
