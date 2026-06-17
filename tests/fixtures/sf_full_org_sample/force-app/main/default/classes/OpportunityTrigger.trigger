trigger OpportunityTrigger on Opportunity (before update) {
    for (Opportunity opp : Trigger.new) {
        if (opp.Amount == null) {
            opp.Amount = 0;
        }
    }
}
