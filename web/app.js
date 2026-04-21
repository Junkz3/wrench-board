// Alpine.js component for the microsolder-agent workbench.
// Handles the WebSocket connection, message state, and send/receive.

function workbench() {
  return {
    ws: null,
    connected: false,
    messages: [],
    draft: "",
    board: { label: "no board loaded" },
    schematic: { label: "no schematic loaded" },

    init() {
      this.connect();
    },

    connect() {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${scheme}://${window.location.host}/ws`;

      const ws = new WebSocket(url);
      this.ws = ws;

      ws.addEventListener("open", () => {
        this.connected = true;
      });

      ws.addEventListener("close", () => {
        this.connected = false;
        // Simple backoff — try again in 2s.
        setTimeout(() => this.connect(), 2000);
      });

      ws.addEventListener("error", () => {
        // Let close handle reconnect.
      });

      ws.addEventListener("message", (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch {
          payload = { type: "message", role: "assistant", text: event.data };
        }

        if (payload.type === "message") {
          this.pushMessage(payload.role || "assistant", payload.text || "");
        }
      });
    },

    send() {
      const text = this.draft.trim();
      if (!text || !this.connected || !this.ws) return;

      this.pushMessage("user", text);
      this.ws.send(JSON.stringify({ type: "message", text }));
      this.draft = "";
    },

    pushMessage(role, text) {
      this.messages.push({ role, text });
      // Scroll to bottom after the DOM updates.
      this.$nextTick(() => {
        const log = this.$refs.log;
        if (log) log.scrollTop = log.scrollHeight;
      });
    },
  };
}

// Expose globally so Alpine's x-data="workbench()" can find it.
window.workbench = workbench;
