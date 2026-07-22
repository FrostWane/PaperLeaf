"use client";

import { create } from "zustand";

export type MobilePane = "pdf" | "ask" | "info";

interface WorkspaceState {
  currentPage: number;
  mobilePane: MobilePane;
  selectedPaperId: string;
  setCurrentPage: (page: number) => void;
  setMobilePane: (pane: MobilePane) => void;
  setSelectedPaperId: (id: string) => void;
  openCitation: (page: number) => void;
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  currentPage: 2,
  mobilePane: "pdf",
  selectedPaperId: "attention",
  setCurrentPage: (currentPage) => set({ currentPage }),
  setMobilePane: (mobilePane) => set({ mobilePane }),
  setSelectedPaperId: (selectedPaperId) => set({ selectedPaperId }),
  openCitation: (currentPage) => set({ currentPage, mobilePane: "pdf" }),
}));
