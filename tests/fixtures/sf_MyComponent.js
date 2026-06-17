import { LightningElement, wire, api } from 'lwc';
import getAccounts from '@salesforce/apex/AccountService.getAccounts';

export default class MyComponent extends LightningElement {
    @api recordId;
    @api label;

    @wire(getAccounts, { name: 'Test' })
    wiredAccounts({ error, data }) {
        if (data) {
            this.accounts = data;
        }
    }

    handleLoadAccounts() {
        // Load accounts
    }
}
