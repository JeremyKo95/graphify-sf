trigger AccountTrigger on Account (before insert, after update) {
    if (Trigger.isBefore) {
        for (Account acc : Trigger.new) {
            acc.Name = acc.Name.trim();
        }
    } else if (Trigger.isAfter) {
        for (Account acc : Trigger.new) {
            List<Opportunity> opps = [SELECT Id FROM Opportunity WHERE AccountId = :acc.Id];
            for (Opportunity opp : opps) {
                opp.Amount = 0;
            }
            update opps;
        }
    }
}
