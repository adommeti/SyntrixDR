import { DeepCard, StatusBadge } from "@drcc/ui";

export default function HomePage() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Syntrix DR Command Center</h1>
      <p className="text-muted-foreground">
        Web shell foundation (BUILD-01). Command Center and My DR screens land in BUILD-12/13, gated on Figma
        sign-off.
      </p>
      <div className="flex gap-2">
        <StatusBadge tone="success" label="Platform healthy" />
        <StatusBadge tone="neutral" label="AI disabled" />
      </div>
      <DeepCard title="Design token / Deep Card plumbing" summary="Proves packages/ui wiring end-to-end.">
        <p>
          This card, its Show More toggle, and the badges above come from{" "}
          <code className="rounded bg-muted px-1 py-0.5">@drcc/ui</code>.
        </p>
      </DeepCard>
    </div>
  );
}
