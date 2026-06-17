/**
 * CPQ Quote Calculator Plugin implementation.
 * Implements SBQQ.QuoteCalculatorPlugin to hook into the Calc Engine.
 */
global class QuoteCalculator implements SBQQ.QuoteCalculatorPlugin {

    global void onBeforePriceRules(SBQQ.QuoteModel quote, List<SBQQ.QuoteLineModel> lines) {
        // Adjust line inputs before price rules run.
    }

    global void onAfterPriceRules(SBQQ.QuoteModel quote, List<SBQQ.QuoteLineModel> lines) {
        // Post-process after price rules.
    }

    global void calculate(SBQQ.QuoteModel quote, List<SBQQ.QuoteLineModel> lines) {
        // Custom calculation logic (core hook).
    }

    global void onAfterCalculate(SBQQ.QuoteModel quote, List<SBQQ.QuoteLineModel> lines) {
        // Final totals adjustments.
    }
}
