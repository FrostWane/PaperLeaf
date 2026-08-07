"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { collectionForest, findCollection } from "@/lib/collections";
import { getDataSource } from "@/lib/data-source";
import { demoCurrentUser, getUserPreferences } from "@/lib/preferences-api";
import type { Paper, PaperCollection } from "@/lib/types";
import { ChatWorkspace, type ChatBinding } from "./chat-workspace";
import { CollectionTree } from "./collection-tree";

const subscribeToClient = () => () => undefined;
type ScopeKey = "all" | string;

export function AskView() {
  const clientReady = useSyncExternalStore(subscribeToClient, () => true, () => false);
  const source = useMemo(() => getDataSource(), []);
  const [scopeKey, setScopeKey] = useState<ScopeKey>("all");
  const [papers, setPapers] = useState<Paper[]>([]);
  const [collections, setCollections] = useState<PaperCollection[]>([]);
  const [scopesLoading, setScopesLoading] = useState(true);
  const [scopesError, setScopesError] = useState("");
  const [webEnabled, setWebEnabled] = useState(process.env.NEXT_PUBLIC_DATA_MODE === "real" ? false : demoCurrentUser.preferences.arxivSearchEnabled);

  useEffect(() => {
    let active = true;
    void Promise.all([source.listPapers(), source.listCollections()])
      .then(([nextPapers, nextCollections]) => {
        if (!active) return;
        setPapers(nextPapers);
        setCollections(collectionForest(nextCollections));
      })
      .catch(() => {
        if (active) setScopesError("文献范围读取失败；仍可使用全部文献提问。");
      })
      .finally(() => {
        if (active) setScopesLoading(false);
      });
    return () => { active = false; };
  }, [source]);

  useEffect(() => {
    if (process.env.NEXT_PUBLIC_DATA_MODE !== "real") return;
    let active = true;
    void getUserPreferences().then((preferences) => { if (active) setWebEnabled(preferences.arxivSearchEnabled); }).catch(() => undefined);
    return () => { active = false; };
  }, []);

  const selectedCollection = useMemo(() => scopeKey === "all" ? undefined : findCollection(collections, scopeKey), [collections, scopeKey]);
  const selectedScopeLabel = selectedCollection?.name ?? "全部文献";
  const allReadyCount = papers.filter((paper) => paper.status === "ready" && !paper.archivedAt).length;
  const binding: ChatBinding = selectedCollection ? { type: "collection", collectionId: selectedCollection.id } : { type: "library" };

  function restoreSessionScope(next: ChatBinding) {
    if (next.type === "library") setScopeKey("all");
    if (next.type === "collection") setScopeKey(next.collectionId);
  }

  return (
    <div className="ask-layout persistent-ask-layout" data-client-ready={clientReady}>
      <aside className="scope-panel">
        <span className="eyebrow">回答范围</span><h2>选择要检索的内容</h2>
        <CollectionTree collections={collections} selectedId={scopeKey} onSelect={setScopeKey} allCount={allReadyCount} disabled={scopesLoading} label="问答集合范围" />
        {scopesError && <p className="field-error" role="alert">{scopesError}</p>}
      </aside>
      <ChatWorkspace
        binding={binding}
        scopeLabel={selectedScopeLabel}
        dataSource={source}
        disabled={!clientReady}
        webEnabled={webEnabled}
        onBindingChange={restoreSessionScope}
        onOpenCitation={(citation) => { window.location.assign(`/library/${citation.paperId}?page=${citation.page}`); }}
      />
    </div>
  );
}
