import { useState, useEffect, useRef } from "react";

/* ─── Design tokens ─────────────────────────────────────────────── */
const C = {
  bg:        "#0f1218",
  panel:     "#181c25",
  panel2:    "#1f2430",
  border:    "#2a3040",
  text:      "#e4e7ed",
  textDim:   "#8993a8",
  textMute:  "#5d6478",
  accent:    "#5eead4",
  accentDim: "#2dd4bf",
  user:      "#60a5fa",
  assistant: "#a78bfa",
  tool:      "#f59e0b",
  error:     "#f87171",
  critical:  "#FF4444",
  warning:   "#FFA500",
  info:      "#4A90E2",
};

/* ─── Helpers ───────────────────────────────────────────────────── */
function severityColor(sev) {
  return (
    { critical: C.critical, error: C.critical, warning: C.warning, info: C.info }[
      (sev || "").toLowerCase()
    ] || C.textDim
  );
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).catch(() => {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  });
}

function openEmail(subject, body) {
  window.open(
    "mailto:?subject=" + encodeURIComponent(subject) +
    "&body="           + encodeURIComponent(body)
  );
}

// function buildInitialPrompt(ctx) {
//   const isSuccess = ["info", "debug"].includes((ctx.severity || "").toLowerCase());
//   return isSuccess
//     ? "I just opened this log entry from Grafana. " +
//       "Order ID: " + ctx.error_id + ", " +
//       "Service: "  + ctx.service  + ", " +
//       "Severity: " + ctx.severity + ". " +
//       "Please give me a quick summary of the status of this order and its milestone progression."
//     : "I just opened this error from Grafana. " +
//       "Order ID: " + ctx.error_id + ", " +
//       "Service: "  + ctx.service  + ", " +
//       "Severity: " + ctx.severity + ". " +
//       "Please give me a quick summary of what this error means and what I should check first.";
// }
function buildInitialPrompt(ctx) {
  const orderId = ctx.error_id || "";

  return orderId === "All"
    ? "Please share a quick summary of CART orders in last 24 hours.."
    : `Please share a quick summary of the status of this order - ${orderId}`;
}

function toApiMessages(msgs) {
  return msgs
    .filter(m => m.role === "user" || m.role === "assistant")
    .map(m => ({ role: m.role, content: m.apiContent || m.content }));
}

/* ─── Session-storage helpers ───────────────────────────────────── */
const STORAGE_KEY       = "ai-chat-history";
const STORAGE_ORDER_KEY = "ai-chat-last-order";

function loadHistory() {
  try { return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]"); }
  catch { return []; }
}

function saveHistory(msgs) {
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(msgs)); }
  catch {}
}

function clearHistory() {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
    sessionStorage.removeItem(STORAGE_ORDER_KEY);
  } catch {}
}

/* ─── API call helper ───────────────────────────────────────────── */
function callBackend(msgs, onSuccess, onError, onDone) {
  fetch("/api/chat", {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ messages: toApiMessages(msgs) }),
  })
  .then(r => {
    if (!r.ok) throw new Error("Backend returned HTTP " + r.status);
    return r.json();
  })
  .then(data => onSuccess(data))
  .catch(err => onError(err))
  .finally(() => onDone());
}

/* ─── Sub-components ────────────────────────────────────────────── */

function ErrorContextCard({ ctx }) {
  const color = severityColor(ctx.severity);
  return (
    <div style={{
      background: C.panel2, border: "1px solid " + color,
      borderRadius: 6, padding: "10px 14px", marginBottom: 12, fontSize: 12,
    }}>
      <div style={{ color, fontWeight: 700, marginBottom: 5, fontSize: 13 }}>
        {ctx.error_id}
      </div>      
    </div>
  );
}

function ActionBtn({ label, onClick }) {
  const [active, setActive] = useState(false);
  return (
    <button
      onClick={() => { onClick(); setActive(true); setTimeout(() => setActive(false), 1200); }}
      style={{
        background: "none",
        border: "1px solid " + (active ? C.accent : C.border),
        color: active ? C.accent : C.textDim,
        padding: "3px 10px", borderRadius: 4, fontSize: 11,
        cursor: "pointer", fontFamily: "inherit", transition: "all 0.15s",
      }}
    >
      {active && label.includes("Copy") ? "✓ Copied" : label}
    </button>
  );
}

function MessageBubble({ msg, onCopy, onEmail }) {
  const isUser  = msg.role === "user";
  const isError = msg.role === "error";
  const roleColor   = isUser ? C.user : isError ? C.error : C.assistant;
  const borderColor = isUser ? C.user : isError ? C.error : C.assistant;
  return (
    <div style={{ marginBottom: 18 }}>
      <div style={{
        fontSize: 10, textTransform: "uppercase", letterSpacing: "0.1em",
        fontWeight: 700, color: roleColor, marginBottom: 5,
      }}>
        {isError ? "error" : msg.role}
      </div>
      <div style={{
        background: C.panel, borderLeft: "3px solid " + borderColor,
        borderRadius: "0 6px 6px 0", padding: "10px 14px",
        whiteSpace: "pre-wrap", wordBreak: "break-word",
        fontSize: 13, color: isError ? C.error : C.text,
        lineHeight: 1.6, textAlign: "left",
      }}>
        {msg.content}
      </div>
      {!isUser && !isError && (
        <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
          <ActionBtn label="📋 Copy" onClick={() => { copyToClipboard(msg.content); onCopy(); }} />
          <ActionBtn label="✉ Email" onClick={() => onEmail(msg.content)} />
        </div>
      )}
    </div>
  );
}

function TracePanel({ trace }) {
  const [open, setOpen] = useState(false);
  if (!trace || trace.length === 0) return null;
  return (
    <div style={{ borderTop: "1px solid " + C.border, marginTop: 6, paddingTop: 8, marginBottom: 16 }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          background: "none", border: "none", color: C.tool,
          fontSize: 11, cursor: "pointer", fontFamily: "inherit", padding: 0,
        }}
      >
        {open ? "▾" : "▸"} tool trace: {trace.map(t => t.tool).join(", ")} (click to {open ? "hide" : "expand"})
      </button>
      {open && (
        <div style={{ marginTop: 8 }}>
          {trace.map((call, i) => (
            <div key={i} style={{
              background: C.panel2, borderLeft: "3px solid " + C.tool,
              padding: "8px 10px", marginBottom: 8,
              borderRadius: "0 4px 4px 0", fontSize: 11,
            }}>
              <div style={{ color: C.tool, fontWeight: 700, fontFamily: "monospace", marginBottom: 3 }}>
                {i + 1}. {call.tool}
              </div>
              <div style={{ color: C.textDim, fontFamily: "monospace", fontSize: 10.5, marginBottom: 4, wordBreak: "break-all" }}>
                {JSON.stringify(call.input)}
              </div>
              <div style={{ color: C.text, fontSize: 11.5 }}>→ {call.result_summary}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Divider({ orderId }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "24px 0 20px" }}>
      <div style={{ flex: 1, height: 1, background: C.border }} />
      <div style={{
        background: C.panel2, border: "1px solid " + C.border,
        borderRadius: 12, padding: "4px 12px",
        fontSize: 11, color: C.textDim, whiteSpace: "nowrap",
      }}>
        🔄 Switched to {orderId}
      </div>
      <div style={{ flex: 1, height: 1, background: C.border }} />
    </div>
  );
}

function Spinner() {
  return (
    <div style={{ color: C.textDim, fontSize: 13, padding: "8px 0", display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{
        display: "inline-block", width: 12, height: 12,
        border: "2px solid " + C.textMute, borderTopColor: C.accent,
        borderRadius: "50%", animation: "spin 0.7s linear infinite",
      }} />
      Analyzing…
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

/* ─── Main App ──────────────────────────────────────────────────── */
export default function App() {
  const params = new URLSearchParams(window.location.search);
  const isHistoryMode = params.get("mode") === "history";

  const errorContext = {
    error_id:    params.get("error_id")   || "ERR-DEMO",
    service:     params.get("service")    || "unknown-service",
    severity:    params.get("severity")   || "error",
    timestamp:   params.get("timestamp")  || new Date().toISOString(),
    environment: params.get("env")        || "production",
  };

  // Clear history if &cleared=1 was passed
  const initialMessages = (() => {
    const cleared = params.get("cleared") === "1";
    if (cleared) clearHistory();
    return isHistoryMode && !cleared ? loadHistory() : [];
  })();

  const [messages, setMessages]   = useState(initialMessages);
  const [input, setInput]         = useState("");
  const [loading, setLoading]     = useState(false);
  const [copyToast, setCopyToast] = useState(false);
  const bottomRef                 = useRef(null);
  const initialSent               = useRef(false);
  const currentCtxRef = useRef(errorContext);

  /* ── Scroll to bottom ── */
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  /* ── Listen for postMessage from Business Text JS ── */
  useEffect(() => {
    function handleMessage(event) {
      if (!event.data) return;

      // Triggered by Close button — clear conversation
      if (event.data.type === "CLEAR_HISTORY") {
        clearHistory();
        setMessages([]);
        return;
      }

      // Triggered by row click — add divider + context card + send new prompt
      if (event.data.type === "NEW_ROW") {
        const newCtx = {
          error_id:    event.data.error_id,
          service:     event.data.service,
          severity:    event.data.severity,
          timestamp:   event.data.timestamp,
          environment: "production",
        };
        currentCtxRef.current = newCtx;

        const divider  = { role: "divider", content: newCtx.error_id };
        const ctxCard  = { role: "context", ctx: newCtx };
        const userMsg  = { role: "user", content: buildInitialPrompt(newCtx) };

        setMessages(prev => {
          const updated = [...prev, divider, ctxCard, userMsg];
          saveHistory(updated);

          setLoading(true);
          callBackend(
            updated,
            (data) => {
              const reply = {
                role: "assistant",
                content: data.answer || "(no answer)",
                trace: data.tool_trace || [],
              };
              setMessages(curr => {
                const final = [...curr, reply];
                saveHistory(final);
                return final;
              });
            },
            (err) => {
              setMessages(curr => {
                const final = [...curr, {
                  role: "error",
                  content: "⚠️ " + err.message,
                  trace: [],
                }];
                saveHistory(final);
                return final;
              });
            },
            () => setLoading(false)
          );

          return updated;
        });
      }
    }
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, []);

  /* ── Auto initial prompt (only on first iframe load) ── */
  useEffect(() => {
    if (initialSent.current) return;
    if (!errorContext.error_id ||
         errorContext.error_id === "none" ||
         errorContext.error_id === "") return;
    initialSent.current = true;

    const prompt  = buildInitialPrompt(errorContext);
    const userMsg = { role: "user", content: prompt };
    const ctxCard = { role: "context", ctx: errorContext };

    let initialMsgs;

    if (isHistoryMode) {
      const saved = loadHistory();
      if (saved.length === 0) {
        initialMsgs = [ctxCard, userMsg];
      } else {
        // Should not happen on first load — history is cleared above if &cleared=1
        initialMsgs = [...saved, { role: "divider", content: errorContext.error_id }, ctxCard, userMsg];
      }
    } else {
      initialMsgs = [ctxCard, userMsg];
    }

    setMessages(initialMsgs);
    if (isHistoryMode) saveHistory(initialMsgs);
    setLoading(true);

    callBackend(
      initialMsgs,
      (data) => {
        const reply = {
          role: "assistant",
          content: data.answer || "(no answer)",
          trace: data.tool_trace || [],
        };
        setMessages(prev => {
          const final = [...prev, reply];
          if (isHistoryMode) saveHistory(final);
          return final;
        });
      },
      (err) => {
        setMessages(prev => {
          const final = [...prev, {
            role: "error",
            content: "⚠️ " + err.message,
            trace: [],
          }];
          if (isHistoryMode) saveHistory(final);
          return final;
        });
      },
      () => setLoading(false)
    );
  }, []);

  /* ── Interactive send ── */
  function handleSend() {
  const userText = input.trim();
  if (!userText || loading) return;

  const ctx = currentCtxRef.current;
  const contextHint =
    "[Context: I am asking about Order ID " + ctx.error_id +
    ", Service " + ctx.service +
    ", Severity " + ctx.severity +
    ", Timestamp " + ctx.timestamp + "]\n\n";

  // Store the clean question for display, full text for backend
  const userMsg = {
    role: "user",
    content: userText,           // shown in chat UI
    apiContent: contextHint + userText, // sent to backend
  };
  const updated = [...messages, userMsg];

    setMessages(updated);
    if (isHistoryMode) saveHistory(updated);
    setInput("");
    setLoading(true);

    callBackend(
      updated,
      (data) => {
        const reply = {
          role: "assistant",
          content: data.answer || "(no answer)",
          trace: data.tool_trace || [],
        };
        setMessages(prev => {
          const final = [...prev, reply];
          if (isHistoryMode) saveHistory(final);
          return final;
        });
      },
      (err) => {
        setMessages(prev => {
          const final = [...prev, {
            role: "error",
            content: "⚠️ " + err.message,
            trace: [],
          }];
          if (isHistoryMode) saveHistory(final);
          return final;
        });
      },
      () => setLoading(false)
    );
  }

  function handleEmail(content) {
    openEmail(
      "AI Log Analysis — " + errorContext.error_id,
      "Order ID: "  + errorContext.error_id + "\n" +
      "Service: "   + errorContext.service   + "\n" +
      "Severity: "  + errorContext.severity  + "\n" +
      "Timestamp: " + errorContext.timestamp + "\n\n" +
      "--- Analysis ---\n\n" + content
    );
  }

  function handleCopy() {
    setCopyToast(true);
    setTimeout(() => setCopyToast(false), 2000);
  }

  /* ── Render ── */
  return (
    <div style={{
      display: "flex", flexDirection: "column", height: "100vh",
      background: C.bg, color: C.text,
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
      fontSize: 13,
    }}>

      <div style={{ flex: 1, overflowY: "auto", padding: "16px 18px" }}>
        {messages.map((msg, i) => {
          if (msg.role === "context") return <ErrorContextCard key={i} ctx={msg.ctx} />;
          if (msg.role === "divider") return <Divider key={i} orderId={msg.content} />;
          return (
            <div key={i}>
              <MessageBubble msg={msg} onCopy={handleCopy} onEmail={handleEmail} />
              {msg.trace && msg.trace.length > 0 && <TracePanel trace={msg.trace} />}
            </div>
          );
        })}
        {loading && <Spinner />}
        <div ref={bottomRef} />
      </div>

      {copyToast && (
        <div style={{
          position: "fixed", bottom: 72, left: "50%", transform: "translateX(-50%)",
          background: C.accent, color: C.bg, padding: "6px 16px",
          borderRadius: 20, fontSize: 12, fontWeight: 600, pointerEvents: "none",
        }}>
          ✓ Copied to clipboard
        </div>
      )}

      <div style={{
        borderTop: "1px solid " + C.border, padding: "12px 18px 16px",
        background: C.panel, flexShrink: 0,
      }}>
        <div style={{ display: "flex", gap: 8 }}>
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
            placeholder="Ask about this error… (Shift+Enter for newline)"
            rows={2}
            style={{
              flex: 1, background: C.bg, border: "1px solid " + C.border,
              color: C.text, padding: "9px 12px", borderRadius: 4,
              resize: "none", fontFamily: "inherit", fontSize: 13,
              lineHeight: 1.5, outline: "none",
            }}
          />
          <button
            onClick={handleSend}
            disabled={loading}
            style={{
              background: loading ? C.panel2 : C.accent,
              color: loading ? C.textMute : C.bg,
              border: "none", padding: "0 18px", borderRadius: 4,
              fontWeight: 700, fontSize: 13,
              cursor: loading ? "not-allowed" : "pointer",
              fontFamily: "inherit", transition: "background 0.15s",
            }}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}