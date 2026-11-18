export function Hero({ onSave }: { onSave: () => void }) {
  return (
    <section className="hero">
      <h1>فروشگاه نمونه</h1>
      <p className="muted" id="hint">این متن کمرنگ است و خوانا نیست</p>
      <div className="actions">
        <button
          className="btn-a"
          id="save"
          onClick={onSave}
        >
          ذخیره
        </button>
        <span className="fake" id="fake-buy">افزودن به سبد</span>
      </div>
    </section>
  );
}
