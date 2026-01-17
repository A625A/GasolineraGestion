"use client";

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";

import ShopProductCard from "@/components/shop/ShopProductCard";
import AutoSaveIndicator from "@/components/ui/AutoSaveIndicator";
import useAutoSaveFeedback from "@/hooks/useAutoSaveFeedback";
import { fetchProducts } from "@/features/shop/api";
import type { ShopProduct } from "@/types/shop";

const ShopPage = () => {
  const { t, i18n } = useTranslation();
  const autoSave = useAutoSaveFeedback({ successDuration: 1500 });
  const { data, isLoading, isError } = useQuery({
    queryKey: ["shop.products"],
    queryFn: fetchProducts,
  });
  const [category, setCategory] = useState<string>("all");

  const categories = useMemo(() => {
    if (!data) {
      return ["all"];
    }
    const unique = Array.from(new Set(data.map((product) => product.category)));
    return ["all", ...unique];
  }, [data]);

  const labels = useMemo(() => ({
    addToCart: t("shop.addToCart"),
    stock: {
      "in-stock": t("shop.stock.available"),
      "low-stock": t("shop.stock.low"),
      "out-of-stock": t("shop.stock.out"),
    },
  }), [t]);

  const filtered = useMemo(() => {
    if (!data) {
      return [] as ShopProduct[];
    }
    if (category === "all") {
      return data;
    }
    return data.filter((product) => product.category === category);
  }, [data, category]);

  const handleAdd = (product: ShopProduct) => {
    autoSave.saved(t("shop.added", { name: product.name }));
  };

  if (isLoading && !data) {
    return (
      <div className="space-y-6">
        <div className="h-6 w-1/3 animate-pulse rounded bg-slate-200" />
        <div className="grid gap-4 md:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <div key={index} className="h-56 animate-pulse rounded-lg bg-slate-200" />
          ))}
        </div>
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="rounded border border-rose-200 bg-white p-4 text-sm text-rose-700">
        {t("shop.error")}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-4 rounded-lg border border-slate-200 bg-white p-4 shadow-sm md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">{t("shop.title")}</h1>
          <p className="text-sm text-slate-500">{t("shop.description")}</p>
        </div>
        <div className="flex items-center gap-3">
          <AutoSaveIndicator status={autoSave.status} labels={{ saving: t("autoSave.saving"), saved: t("autoSave.saved"), error: t("autoSave.error") }} message={autoSave.message} />
          <select
            value={category}
            onChange={(event) => setCategory(event.target.value)}
            className="rounded border border-slate-200 px-3 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/40"
          >
            {categories.map((item) => (
              <option key={item} value={item}>
                {item === "all" ? t("shop.categories.all") : item}
              </option>
            ))}
          </select>
        </div>
      </header>

      <div className="grid gap-4 md:grid-cols-3">
        {filtered.map((product) => (
          <ShopProductCard key={product.id} product={product} labels={labels} onAddToCart={handleAdd} />
        ))}
      </div>

      {filtered.length === 0 ? (
        <p className="text-sm text-slate-500">{t("shop.emptyCategory")}</p>
      ) : null}
    </div>
  );
};

export default ShopPage;
