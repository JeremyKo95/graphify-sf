public class OpportunityService {
    public static List<Opportunity> getOpenOpportunities() {
        return [SELECT Id, Amount FROM Opportunity WHERE StageName != 'Closed'];
    }

    public static void closeOpportunities(List<Id> oppIds) {
        List<Opportunity> opps = [SELECT Id FROM Opportunity WHERE Id IN :oppIds];
        for (Opportunity opp : opps) {
            List<Account> related = [SELECT Id FROM Account WHERE Id = :opp.AccountId];
            opp.StageName = 'Closed Won';
        }
        update opps;
    }
}
