"use client";

import clsx from "classnames";

import type { ShopProduct } from "@/types/shop";

interface ShopProductCardProps {
  product: ShopProduct;
  labels: {
    addToCart: string;
    stock: Record<ShopProduct["stockStatus"], string>;
  };
  onAddToCart?: (product: ShopProduct) => void;
}

const stockBadgeClasses: Record<ShopProduct["stockStatus"], string> = {
  "in-stock": "bg-emerald-100 text-emerald-700",
  "low-stock": "bg-amber-100 text-amber-700",
  "out-of-stock": "bg-rose-100 text-rose-700",
};

const ShopProductCard = ({ product, labels, onAddToCart }: ShopProductCardProps) => {
  const handleAdd = () => {
    if (product.stockStatus === "out-of-stock") {
      return;
    }
    onAddToCart?.(product);
  };

  return (
    <article className="flex flex-col rounded-lg border border-slate-200 bg-white shadow-sm transition hover:-translate-y-0.5 hover:shadow-md">
      <div
        className="h-40 w-full rounded-t-lg bg-gradient-to-br from-slate-200 to-slate-100"
        style={{
          backgroundImage: `linear-gradient(135deg, rgba(15, 118, 110, 0.15), rgba(67, 56, 202, 0.15)), url(${product.image})`,
          backgroundSize: "cover",
          backgroundPosition: "center",
        }}
      />
      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wide text-primary">
            {product.category}
          </span>
          <span
            className={clsx(
              "rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
              stockBadgeClasses[product.stockStatus]
            )}
          >
            {labels.stock[product.stockStatus]}
          </span>
        </div>
        <div>
          <h3 className="text-base font-semibold text-slate-900">{product.name}</h3>
          <p className="text-sm text-slate-500">Q {product.price.toFixed(2)}</p>
        </div>
        <button
          type="button"
          onClick={handleAdd}
          disabled={product.stockStatus === "out-of-stock"}
          className={clsx(
            "mt-auto rounded border px-3 py-2 text-sm font-semibold transition",
            product.stockStatus === "out-of-stock"
              ? "border-slate-200 text-slate-400"
              : "border-primary text-primary hover:bg-primary/10"
          )}
        >
          {labels.addToCart}
        </button>
      </div>
    </article>
  );
};

export default ShopProductCard;
