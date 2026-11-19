# Seed to Profit

Start a company and make it worth as much as you can. Your team has $2,000,000 of seed money to start a consumer goods company that sells physical products online, on its own site and on a marketplace. You choose what to sell from a catalog of 30 products (`company/plan.yaml`), and you write the software that runs the company every day: prices, ads, stock orders and money, and how the company grows: own improved products, retail chains, subscriptions and bundles, raising money, and selling in other countries, which the company can enter and leave during the run (`ops/`). A market office sells research about the products and the market, and runs your software in practice markets, within a fixed research budget. `docs/REFERENCE.md` is the manual: read it first.

After a launch phase, your company opens and its three years run while you work, a month every few minutes, each month with the software on `main` at that moment. Watch it through its books and change the software as you learn. Your investors are also betting on what you learn: the software and plan you leave behind are a playbook they can use to start and grow more companies like yours. Half the score is what your company is worth when its three years end; half is how 24 new companies do when your final `main` starts and runs them on its own, in new markets, after the run.

The section "The market" in `docs/REFERENCE.md` lists every model the market uses. You never see the market's code, so write your software for each of them, and for markets that change in ways you are not told about.

Push your work to `main` early and often. Your company and the office's `plan_review` and `test_market` read `main` (or the ref you give them): work that is only committed in your own clone does not count.
