import { expect, test } from "@playwright/test";

const apiKey = process.env.FORGEEVAL_API_KEY;

// Needs a stack seeded by examples/triage/seed.py plus one regressed run (fake/echo): the newest
// run uses prompt triage@2, label-match fails on every item and the baseline run scored 1.0.
test("sign in, open a regressed run, read a failing case, compare with baseline", async ({
  page,
}) => {
  expect(apiKey, "set FORGEEVAL_API_KEY to a project API key").toBeTruthy();

  // Sign in with a project API key.
  await page.goto("/");
  await page.getByLabel("API key").fill(apiKey ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();

  // Runs list: open the regressed run.
  await expect(page.getByRole("heading", { name: "Runs", level: 1 })).toBeVisible();
  const regressed = page.getByRole("row").filter({ hasText: "triage@2" }).first();
  await expect(regressed).toBeVisible();
  await regressed.getByRole("link", { name: /^[0-9a-f]{8}$/ }).click();
  await expect(page.getByRole("heading", { name: "Metrics" })).toBeVisible();
  const runUrl = page.url();

  // Items view, filtered to the failing evaluator, shows a failing case with its rationale.
  await page
    .getByRole("link", { name: /failing cases?/ })
    .first()
    .click();
  await expect(page.getByLabel("Failed evaluator")).toHaveValue("label-match");
  await page
    .getByRole("button", { name: /expand item/i })
    .first()
    .click();
  const detail = page.getByTestId("item-detail").first();
  await expect(detail).toBeVisible();
  await expect(detail.getByRole("region", { name: "Input" })).toBeVisible();
  await expect(detail.getByRole("region", { name: "Expected" })).toBeVisible();
  await expect(detail.getByRole("region", { name: "Actual output" })).toBeVisible();
  await expect(detail.getByText("fail", { exact: true })).toBeVisible();

  // Compare page shows a regressed evaluator.
  await page.goto(`${runUrl}/compare`);
  const baseline = page.getByLabel(/Baseline run/);
  if ((await baseline.inputValue()) === "") {
    // The CI gate run has no stored baseline_run_id: pick the seeded baseline (prompt triage@1).
    const value = await baseline
      .locator("option", { hasText: "triage@1" })
      .first()
      .getAttribute("value");
    await baseline.selectOption(value ?? "");
  }
  const row = page.getByRole("row").filter({ hasText: "label-match@1" });
  await expect(row.getByText("regressed", { exact: true })).toBeVisible();
  await expect(row.getByRole("img", { name: /regressed/ })).toBeVisible();
  await expect(page.getByTestId("newly-failing")).toContainText("label-match");
});
