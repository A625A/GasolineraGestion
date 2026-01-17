"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";

import ShopProductCard from "@/components/shop/ShopProductCard";
import AutoSaveIndicator from "@/components/ui/AutoSaveIndicator";
import useAutoSaveFeedback from "@/hooks/useAutoSaveFeedback";
import { fetchProducts } from "@/features/shop/api";
import type { ShopProduct } from "@/types/shop";

const ShopPanel = () => {
  const { t, i18n } = useTranslation();
  const autoSave = useAutoSaveFeedback({ successDuration: 1500 });
  const { data, isLoading, isError } = useQuery({
    queryKey: ["shop.products"],
    queryFn: fetchProducts,
  });
  const [lastAdded, setLastAdded] = useState<ShopProduct | null>(null);

  const labels = useMemo(() => ({
    addToCart: t("shop.addToCart"),
    stock: {
      "in-stock": t("shop.stock.available"),
      "low-stock": t("shop.stock.low"),
      "out-of-stock": t("shop.stock.out"),
    },
  }), [t]);

  const handleAdd = (product: ShopProduct) => {
    setLastAdded(product);
    autoSave.saved(t("shop.added", { name: product.name }));
  };

  if (isLoading && !data) {
    return (
      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <div className="h-4 w-1/3 animate-pulse rounded bg-slate-200" />
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <div className="h-40 animate-pulse rounded bg-slate-200" />
          <div className="h-40 animate-pulse rounded bg-slate-200" />
          <div className="h-40 animate-pulse rounded bg-slate-200" />
        </div>
      </section>
    );
  }

  if (isError || !data) {
    return (
      <section className="rounded-lg border border-rose-200 bg-white p-4 shadow-sm">
        <h2 className="text-lg font-semibold text-slate-800">{t("shop.title")}</h2>
        <p className="mt-2 text-sm text-rose-600">{t("shop.error")}</p>
      </section>
    );
  }

  const featured = data.slice(0, 3);

  return (
    <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <header className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-slate-800">{t("shop.title")}</h2>
          <p className="text-xs text-slate-500">{t("shop.subtitle")}</p>
        </div>
        <div className="flex items-center gap-2">
          <AutoSaveIndicator status={autoSave.status} labels={{ saving: t("autoSave.saving"), saved: t("autoSave.saved"), error: t("autoSave.error") }} message={autoSave.message} />
          <Link href="/shop" className="text-xs font-semibold text-primary hover:underline">
            {t("shop.viewAll")}
          </Link>
        </div>
      </header>
      <div className="grid gap-3 sm:grid-cols-3">
        {featured.map((product) => (
          <ShopProductCard key={product.id} product={product} labels={labels} onAddToCart={handleAdd} />
        ))}
      </div>
      {lastAdded ? (
        <p className="text-xs text-slate-500">
          {t("shop.lastAdded", { name: lastAdded.name, price: lastAdded.price.toLocaleString(i18n.language, { minimumFractionDigits: 2 }) })}
        </p>
      ) : null}
    </section>
  );
};

export default ShopPanel;
