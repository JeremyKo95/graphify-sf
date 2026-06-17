public class AccountService {
    public static List<Account> getAccounts(String name) {
        List<Account> accounts = [SELECT Id, Name FROM Account WHERE Name = :name];
        for (Account acc : accounts) {
            if (acc.Name.contains('Test')) {
                return accounts;
            }
        }
        return accounts;
    }

    public static void updateAccount(Account acc) {
        update acc;
    }
}
