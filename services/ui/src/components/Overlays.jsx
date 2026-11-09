// Modal dialogs, the touch keypad and toast messages. No physical keyboard on the kiosk.
import React, { useEffect, useState } from "react";

export function Modal({ title, subtitle, onClose, children, wide }) {
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onPointerDown={(e) => e.target === e.currentTarget && onClose && onClose()}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <div>
            <div className="modal-title">{title}</div>
            {subtitle && <div className="modal-sub">{subtitle}</div>}
          </div>
          {onClose && (
            <button className="btn btn-ghost btn-close" onClick={onClose} aria-label="Close">
              ✕
            </button>
          )}
        </div>
        {children}
      </div>
    </div>
  );
}

export function Confirm({ title, message, confirmLabel = "Confirm", tone = "primary", onConfirm, onClose, busy }) {
  return (
    <Modal title={title} onClose={onClose}>
      <p className="modal-text">{message}</p>
      <div className="modal-actions">
        <button className="btn btn-lg" onClick={onClose}>
          Cancel
        </button>
        <button className={`btn btn-lg btn-${tone}`} onClick={onConfirm} disabled={busy}>
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

const KEYS = ["7", "8", "9", "4", "5", "6", "1", "2", "3", "C", "0", "⌫"];

/**
 * Touch keypad for whole numbers.
 * actions: [{ label, tone, onClick(value), disabled, note }]
 */
export function Keypad({ title, subtitle, initial, min = 1, max = 99999, actions, onClose, note }) {
  const [text, setText] = useState(String(initial ?? ""));
  const [fresh, setFresh] = useState(true); // first key press replaces the shown value
  const value = text === "" ? NaN : parseInt(text, 10);
  const valid = Number.isInteger(value) && value >= min && value <= max;

  const press = (k) => {
    if (k === "C") {
      setText("");
    } else if (k === "⌫") {
      setText((t) => (fresh ? "" : t.slice(0, -1)));
    } else {
      setText((t) => {
        const base = fresh ? "" : t;
        const next = (base + k).replace(/^0+(?=\d)/, "");
        return next.length > String(max).length ? base : next;
      });
    }
    setFresh(false);
  };

  useEffect(() => {
    const onKey = (e) => {
      if (/^[0-9]$/.test(e.key)) press(e.key);
      else if (e.key === "Backspace") press("⌫");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <Modal title={title} subtitle={subtitle} onClose={onClose}>
      <div className={`keypad-display ${fresh ? "is-fresh" : ""} ${text !== "" && !valid ? "is-invalid" : ""}`}>
        {text === "" ? <span className="placeholder">—</span> : text}
      </div>
      <div className="keypad-range">
        {text !== "" && !valid ? `Enter a value from ${min} to ${max}` : `Range ${min} – ${max}`}
      </div>
      <div className="keypad">
        {KEYS.map((k) => (
          <button key={k} className={`key ${k === "C" || k === "⌫" ? "key-fn" : ""}`} onClick={() => press(k)}>
            {k}
          </button>
        ))}
      </div>
      {note && <div className="keypad-note">{note}</div>}
      <div className="modal-actions">
        {actions.map((a) => (
          <button
            key={a.label}
            className={`btn btn-lg btn-${a.tone || "neutral"}`}
            disabled={!valid || a.disabled}
            title={a.disabled ? a.note : undefined}
            onClick={() => a.onClick(value)}
          >
            {a.label}
          </button>
        ))}
      </div>
    </Modal>
  );
}

export function Toasts({ items, onDismiss }) {
  return (
    <div className="toasts" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={`toast toast-${t.tone}`} onClick={() => onDismiss(t.id)}>
          {t.text}
        </div>
      ))}
    </div>
  );
}
