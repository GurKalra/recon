import { EmptyState, PageHeader } from "../components/ui/Section";

export function Soon({ title, lead }: { title: string; lead: string }) {
  return (
    <>
      <PageHeader title={title} lead={lead} />
      <div className="mt-10">
        <EmptyState title="Coming in the next batch">
          This screen is built after the Overview.
        </EmptyState>
      </div>
    </>
  );
}
