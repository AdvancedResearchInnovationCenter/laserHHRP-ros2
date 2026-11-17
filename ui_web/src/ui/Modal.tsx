import type { ReactNode } from "react";

export function Modal({ title, children, onClose, wide }: {
  title: string; children: ReactNode; onClose?: () => void; wide?: boolean;
}) {
  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className={wide ? "modal wide" : "modal"} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          {onClose && <button className="ghost close" aria-label="Close" onClick={onClose}>✕</button>}
        </div>
        {children}
      </div>
    </div>
  );
}

export function Confirm({ title, text, confirmLabel, tone = "primary", onConfirm, onCancel }: {
  title: string; text: ReactNode; confirmLabel: string; tone?: "primary" | "danger";
  onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <Modal title={title} onClose={onCancel}>
      <div className="modal-text">{text}</div>
      <div className="modal-actions">
        <button className="secondary" onClick={onCancel}>Cancel</button>
        <button className={tone} onClick={onConfirm}>{confirmLabel}</button>
      </div>
    </Modal>
  );
}
